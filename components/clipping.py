import torch
from .long_clip_model import longclip
from comfy.sd1_clip import load_embed, ClipTokenWeightEncoder
from comfy.sd1_clip import token_weights, escape_important, unescape_important

class SDLongClipModel(torch.nn.Module, ClipTokenWeightEncoder):
    LAYERS = [
        "last",
        "pooled",
        "hidden"
    ]

    def __init__(self, version="openai/clip-vit-large-patch14", device="cpu", max_length=77, freeze=True, layer="last", layer_idx=None, dtype=None, special_tokens={"start": 49406, "end": 49407, "pad": 49407}, layer_norm_hidden_state=True, enable_attention_masks=False, return_projected_pooled=True):
        super().__init__()
        assert layer in self.LAYERS
        self.transformer, _ = longclip.load(version, device=device)
        self.num_layers = self.transformer.transformer_layers
        self.max_length = max_length
        if freeze:
            self.freeze()
        self.layer = layer
        self.layer_idx = None
        self.special_tokens = special_tokens
        self.text_projection = torch.nn.Parameter(torch.eye(self.transformer.get_input_embeddings().weight.shape[1]))
        self.logit_scale = torch.nn.Parameter(torch.tensor(4.6055))
        self.enable_attention_masks = enable_attention_masks
        self.layer_norm_hidden_state = layer_norm_hidden_state
        self.return_projected_pooled = return_projected_pooled
        if layer == "hidden":
            assert layer_idx is not None
            assert abs(layer_idx) < self.num_layers
            self.clip_layer(layer_idx)
        self.layer_default = (self.layer, self.layer_idx)
        self.options_default = (self.layer, self.layer_idx, self.return_projected_pooled)

    def freeze(self):
        self.transformer = self.transformer.eval()
        for param in self.parameters():
            param.requires_grad = False

    def clip_layer(self, layer_idx):
        if abs(layer_idx) > self.num_layers:
            self.layer = "last"
        else:
            self.layer = "hidden"
            self.layer_idx = layer_idx

    def reset_clip_layer(self):
        self.layer = self.layer_default[0]
        self.layer_idx = self.layer_default[1]

    def set_clip_options(self, options):
        layer_idx = options.get("layer", self.layer_idx)
        self.return_projected_pooled = options.get("projected_pooled", self.return_projected_pooled)
        if layer_idx is None or abs(layer_idx) > self.num_layers:
            self.layer = "last"
        else:
            self.layer = "hidden"
            self.layer_idx = layer_idx

    def reset_clip_options(self):
        self.layer = self.options_default[0]
        self.layer_idx = self.options_default[1]
        self.return_projected_pooled = self.options_default[2]

    def set_up_textual_embeddings(self, tokens, current_embeds):
        out_tokens = []
        next_new_token = token_dict_size = current_embeds.weight.shape[0] - 1
        embedding_weights = []

        for x in tokens:
            tokens_temp = []
            for y in x:
                if isinstance(y, int):
                    if y == token_dict_size:
                        y = -1
                    tokens_temp += [y]
                else:
                    if y.shape[0] == current_embeds.weight.shape[1]:
                        embedding_weights += [y]
                        tokens_temp += [next_new_token]
                        next_new_token += 1
                    else:
                        print("WARNING: shape mismatch when trying to apply embedding, embedding will be ignored", y.shape[0], current_embeds.weight.shape[1])
            while len(tokens_temp) < len(x):
                tokens_temp += [self.special_tokens["pad"]]
            out_tokens += [tokens_temp]

        n = token_dict_size
        if len(embedding_weights) > 0:
            new_embedding = torch.nn.Embedding(next_new_token + 1, current_embeds.weight.shape[1], device=current_embeds.weight.device, dtype=current_embeds.weight.dtype)
            new_embedding.weight[:token_dict_size] = current_embeds.weight[:-1]
            for x in embedding_weights:
                new_embedding.weight[n] = x
                n += 1
            new_embedding.weight[n] = current_embeds.weight[-1]
            self.transformer.set_input_embeddings(new_embedding)

        processed_tokens = []
        for x in out_tokens:
            processed_tokens += [list(map(lambda a: n if a == -1 else a, x))]
        return processed_tokens

    def forward(self, tokens):
        backup_embeds = self.transformer.get_input_embeddings()
        device = backup_embeds.weight.device
        tokens = self.set_up_textual_embeddings(tokens, backup_embeds)
        tokens = torch.LongTensor(tokens).to(device)

        attention_mask = None
        if self.enable_attention_masks:
            attention_mask = torch.zeros_like(tokens)
            max_token = self.transformer.get_input_embeddings().weight.shape[0] - 1
            for x in range(attention_mask.shape[0]):
                for y in range(attention_mask.shape[1]):
                    attention_mask[x, y] = 1
                    if tokens[x, y] == max_token:
                        break

        outputs = self.transformer(tokens, attention_mask, intermediate_output=self.layer_idx, final_layer_norm_intermediate=self.layer_norm_hidden_state)
        self.transformer.set_input_embeddings(backup_embeds)

        if self.layer == "last":
            z = outputs[0]
        else:
            z = outputs[1]

        pooled_output = None
        if len(outputs) >= 3:
            if not self.return_projected_pooled and len(outputs) >= 4 and outputs[3] is not None:
                pooled_output = outputs[3].float()
            elif outputs[2] is not None:
                pooled_output = outputs[2].float()

        return z.float(), pooled_output

    def encode(self, tokens):
        return self(tokens)

    def load_sd(self, sd):
        if "text_projection" in sd:
            self.text_projection[:] = sd.pop("text_projection")
        if "text_projection.weight" in sd:
            self.text_projection[:] = sd.pop("text_projection.weight").transpose(0, 1)
        return self.transformer.load_state_dict(sd, strict=False)

class SDLongTokenizer:
    def __init__(self, max_length=248, pad_with_end=True, embedding_directory=None, embedding_size=768, embedding_key='clip_l',  has_start_token=True, pad_to_max_length=True):
        self.tokenizer = longclip.only_tokenize
        self.max_length = max_length
        empty = self.tokenizer('')[0]
        if has_start_token:
            self.tokens_start = 1
            self.start_token = empty[0]
            self.end_token = empty[1]
        else:
            self.tokens_start = 0
            self.start_token = None
            self.end_token = empty[0]
        self.pad_with_end = pad_with_end
        self.pad_to_max_length = pad_to_max_length
        self.embedding_directory = embedding_directory
        self.max_word_length = 8
        self.embedding_identifier = "embedding:"
        self.embedding_size = embedding_size
        self.embedding_key = embedding_key

    def _try_get_embedding(self, embedding_name:str):
        embed = load_embed(embedding_name, self.embedding_directory, self.embedding_size, self.embedding_key)
        if embed is None:
            stripped = embedding_name.strip(',')
            if len(stripped) < len(embedding_name):
                embed = load_embed(stripped, self.embedding_directory, self.embedding_size, self.embedding_key)
                return (embed, embedding_name[len(stripped):])
        return (embed, "")

    def tokenize_with_weights(self, text:str, return_word_ids=False):
        if self.pad_with_end:
            pad_token = self.end_token
        else:
            pad_token = 0

        text = escape_important(text)
        parsed_weights = token_weights(text, 1.0)

        tokens = []
        for weighted_segment, weight in parsed_weights:
            to_tokenize = unescape_important(weighted_segment).replace("\n", " ").split(' ')
            to_tokenize = [x for x in to_tokenize if x != ""]
            for word in to_tokenize:
                if word.startswith(self.embedding_identifier) and self.embedding_directory is not None:
                    embedding_name = word[len(self.embedding_identifier):].strip('\n')
                    embed, leftover = self._try_get_embedding(embedding_name)
                    if embed is None:
                        print(f"warning, embedding:{embedding_name} does not exist, ignoring")
                    else:
                        if len(embed.shape) == 1:
                            tokens.append([(embed, weight)])
                        else:
                            tokens.append([(embed[x], weight) for x in range(embed.shape[0])])
                    if leftover != "":
                        word = leftover
                    else:
                        continue
                tokens.append([(t, weight) for t in self.tokenizer(word)[0][self.tokens_start:-1]])

        batched_tokens = []
        batch = []
        if self.start_token is not None:
            batch.append((self.start_token, 1.0, 0))
        batched_tokens.append(batch)
        for i, t_group in enumerate(tokens):
            is_large = len(t_group) >= self.max_word_length

            while len(t_group) > 0:
                if len(t_group) + len(batch) > self.max_length - 1:
                    remaining_length = self.max_length - len(batch) - 1
                    if is_large:
                        batch.extend([(t,w,i+1) for t,w in t_group[:remaining_length]])
                        batch.append((self.end_token, 1.0, 0))
                        t_group = t_group[remaining_length:]
                    else:
                        batch.append((self.end_token, 1.0, 0))
                        if self.pad_to_max_length:
                            batch.extend([(pad_token, 1.0, 0)] * (remaining_length))
                    batch = []
                    if self.start_token is not None:
                        batch.append((self.start_token, 1.0, 0))
                    batched_tokens.append(batch)
                else:
                    batch.extend([(t,w,i+1) for t,w in t_group])
                    t_group = []

        batch.append((self.end_token, 1.0, 0))
        if self.pad_to_max_length:
            batch.extend([(pad_token, 1.0, 0)] * (self.max_length - len(batch)))

        if not return_word_ids:
            batched_tokens = [[(t, w) for t, w,_ in x] for x in batched_tokens]
        return batched_tokens

    def untokenize(self, token_weight_pair):
        return list(map(lambda a: (a, self.inv_vocab[a[0]]), token_weight_pair))

def pad_tokens(tokens,clip,add_token_num):
    if clip.pad_with_end:
        pad_token = clip.end_token
    else:
        pad_token = 0
    while add_token_num > 0:
        batch = []
        batch.append((clip.end_token, 1.0, 0))
        add_pad = clip.max_length - 1
        batch.extend([(pad_token, 1.0, 0)] * add_pad)
        tokens.append(batch)
        add_token_num -= (add_pad+1)
    return tokens

def token_num(tokens):
    n = 0
    for token in tokens:
        n += len(token)
    return n

class SDXLLongClipModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.clip_l = None
        self.clip_g = None

    def set_clip_options(self, options):
        self.clip_l.set_clip_options(options)
        self.clip_g.set_clip_options(options)

    def reset_clip_options(self):
        self.clip_g.reset_clip_options()
        self.clip_l.reset_clip_options()

    def encode_token_weights(self, token_weight_pairs):
        token_weight_pairs_g = token_weight_pairs["g"]
        token_weight_pairs_l = token_weight_pairs["l"]
        g_out, g_pooled = self.clip_g.encode_token_weights(token_weight_pairs_g)
        l_out, l_pooled = self.clip_l.encode_token_weights(token_weight_pairs_l)
        g_tokens = g_out.shape[1]
        l_tokens = l_out.shape[1]
        min_tokens = min(g_tokens,l_tokens)
        g_out = g_out[:,:min_tokens,:]
        l_out = l_out[:,:min_tokens,:]
        return torch.cat([l_out, g_out], dim=-1), g_pooled

    def load_sd(self, sd):
        if "text_model.encoder.layers.30.mlp.fc1.weight" in sd:
            return self.clip_g.load_sd(sd)
        else:
            return self.clip_l.load_sd(sd)

class SDXLLongTokenizer:
    def __init__(self):
        self.clip_l = None
        self.clip_g = None

    def tokenize_with_weights(self, text:str, return_word_ids=False):
        out = {}
        out["g"] = self.clip_g.tokenize_with_weights(text, return_word_ids)
        out["l"] = self.clip_l.tokenize_with_weights(text, return_word_ids)
        g_tokens = token_num(out["g"])
        l_tokens = token_num(out["l"])
        if g_tokens > l_tokens:
            out["l"] = pad_tokens(out["l"],self.clip_l,g_tokens-l_tokens)
        elif l_tokens > g_tokens:
            out["g"] = pad_tokens(out["g"],self.clip_g,l_tokens-g_tokens)
        return out

    def untokenize(self, token_weight_pair):
        return self.clip_g.untokenize(token_weight_pair)
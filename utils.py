import os
import sys
import PIL.Image
import PIL.ImageOps
import requests
import torch
from typing import List, Union
from collections import namedtuple
from .model import PhotoMakerIDEncoder
from comfy.sd1_clip import escape_important, token_weights, unescape_important

Hook = namedtuple('Hook', ['fn', 'module_name', 'target', 'orig_key', 'module_name_nt', 'module_name_unix'])

def hook_clip_model_CLIPVisionModelProjection():
    return create_hook(PhotoMakerIDEncoder, 'comfy.clip_model', 'CLIPVisionModelProjection')

def hhok_unclip_adm():
    import comfy.model_base
    comfy.model_base.unclip_adm_original = comfy.model_base.unclip_adm
    return create_hook(unclip_adm, 'comfy.model_base')

def hook_tokenize_with_weights():
    import comfy.sd1_clip
    comfy.sd1_clip.SDTokenizer.tokenize_with_weights_original = comfy.sd1_clip.SDTokenizer.tokenize_with_weights
    comfy.sd1_clip.SDTokenizer.tokenize_with_weights = tokenize_with_weights
    return create_hook(tokenize_with_weights, 'comfy.sd1_clip', 'SDTokenizer.tokenize_with_weights')

def create_hook(fn, module_name, target = None, orig_key = None):
    if target is None: target = fn.__name__
    if orig_key is None: orig_key = f'{target}_original'
    module_name_nt = '\\'.join(module_name.split('.'))
    module_name_unix = '/'.join(module_name.split('.'))
    return Hook(fn, module_name, target, orig_key, module_name_nt, module_name_unix)

def hook_all(restore=False):
    hooks: List[Hook] = [
        hook_clip_model_CLIPVisionModelProjection(),
        hook_tokenize_with_weights(),
        hhok_unclip_adm(),
    ]
    for m in sys.modules.keys():
        for hook in hooks:
            if hook.module_name == m or (os.name != 'nt' and m.endswith(hook.module_name_unix)) or (os.name == 'nt' and m.endswith(hook.module_name_nt)):
                if hasattr(sys.modules[m], hook.target):
                    if not hasattr(sys.modules[m], hook.orig_key):
                        if (orig_fn:=getattr(sys.modules[m], hook.target, None)) is not None:
                            setattr(sys.modules[m], hook.orig_key, orig_fn)
                    if restore:
                        setattr(sys.modules[m], hook.target, getattr(sys.modules[m], hook.orig_key, None))
                    else:
                        setattr(sys.modules[m], hook.target, hook.fn)


def unclip_adm(unclip_conditioning, device, noise_augmentor, noise_augment_merge=0.0):
    adm_inputs = []
    weights = []
    noise_aug = []
    for unclip_cond in unclip_conditioning:
        for adm_cond in unclip_cond["clip_vision_output"].image_embeds:
            weight = unclip_cond["strength"]
            noise_augment = unclip_cond["noise_augmentation"]
            noise_level = round((noise_augmentor.max_noise_level - 1) * noise_augment)
            # c_adm, noise_level_emb = noise_augmentor(adm_cond.to(device), noise_level=torch.tensor([noise_level], device=device))
            # adm_out = torch.cat((c_adm, noise_level_emb), 1) * weight

            # if c_adm.shape[0] > 1:
            #     c_adm = c_adm[:1, :]
            # c_adm, noise_level_emb = noise_augmentor(adm_cond.to(device), noise_level=torch.tensor([noise_level], device=device))
            # adm_out = torch.cat((c_adm, noise_level_emb), 1) * weight

            i=1
            data_mean = noise_augmentor.data_mean.repeat(1, -(adm_cond.shape[i] // -noise_augmentor.data_mean.shape[1]))[:, :adm_cond.shape[i]]
            data_std = noise_augmentor.data_std.repeat(1, -(adm_cond.shape[i] // -noise_augmentor.data_std.shape[1]))[:, :adm_cond.shape[i]]
            noise_augmentor.register_buffer("data_mean", data_mean, persistent=False)
            noise_augmentor.register_buffer("data_std", data_std, persistent=False)
            # noise_augmentor.data_mean = data_mean
            # noise_augmentor.data_std = data_std
            c_adm, noise_level_emb = noise_augmentor(adm_cond.to(device), noise_level=torch.tensor([noise_level], device=device))
            tmp = noise_level_emb.repeat(-(c_adm.shape[0] // -noise_level_emb.shape[0]),1)[:c_adm.shape[0]]
            adm_out = torch.cat((c_adm, tmp), 1) * weight

            weights.append(weight)
            noise_aug.append(noise_augment)
            adm_inputs.append(adm_out)

    if len(noise_aug) > 1:
        adm_out = torch.stack(adm_inputs).sum(0)
        noise_augment = noise_augment_merge
        noise_level = round((noise_augmentor.max_noise_level - 1) * noise_augment)
        c_adm, noise_level_emb = noise_augmentor(adm_out[:, :noise_augmentor.time_embed.dim], noise_level=torch.tensor([noise_level], device=device))
        adm_out = torch.cat((c_adm, noise_level_emb), 1)

    return adm_out

def tokenize_with_weights(self, text:str, return_word_ids=False, _tokens=[], return_tokens=False):
    '''
    Takes a prompt and converts it to a list of (token, weight, word id) elements.
    Tokens can both be integer tokens and pre computed CLIP tensors.
    Word id values are unique per word and embedding, where the id 0 is reserved for non word tokens.
    Returned list has the dimensions NxM where M is the input size of CLIP
    '''
    if self.pad_with_end:
        pad_token = self.end_token
    else:
        pad_token = 0

    tokens = _tokens
    if len(_tokens) == 0:
        text = escape_important(text)
        parsed_weights = token_weights(text, 1.0)

        #tokenize words
        tokens = []
        for weighted_segment, weight in parsed_weights:
            to_tokenize = unescape_important(weighted_segment).replace("\n", " ").split(' ')
            to_tokenize = [x for x in to_tokenize if x != ""]
            for word in to_tokenize:
                #if we find an embedding, deal with the embedding
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
                    #if we accidentally have leftover text, continue parsing using leftover, else move on to next word
                    if leftover != "":
                        word = leftover
                    else:
                        continue
                #parse word
                tokens.append([(t, weight) for t in self.tokenizer(word)["input_ids"][self.tokens_start:-1]])
    if return_tokens: return tokens

    #reshape token array to CLIP input size
    batched_tokens = []
    batch = []
    if self.start_token is not None:
        batch.append((self.start_token, 1.0, 0))
    batched_tokens.append(batch)
    for i, t_group in enumerate(tokens):
        #determine if we're going to try and keep the tokens in a single batch
        is_large = len(t_group) >= self.max_word_length

        while len(t_group) > 0:
            if len(t_group) + len(batch) > self.max_length - 1:
                remaining_length = self.max_length - len(batch) - 1
                #break word in two and add end token
                if is_large:
                    batch.extend([(t,w,i+1) for t,w in t_group[:remaining_length]])
                    batch.append((self.end_token, 1.0, 0))
                    t_group = t_group[remaining_length:]
                #add end token and pad
                else:
                    batch.append((self.end_token, 1.0, 0))
                    if self.pad_to_max_length:
                        batch.extend([(pad_token, 1.0, 0)] * (remaining_length))
                #start new batch
                batch = []
                if self.start_token is not None:
                    batch.append((self.start_token, 1.0, 0))
                batched_tokens.append(batch)
            else:
                batch.extend([(t,w,i+1) for t,w in t_group])
                t_group = []

    #fill last batch
    batch.append((self.end_token, 1.0, 0))
    if self.pad_to_max_length:
        batch.extend([(pad_token, 1.0, 0)] * (self.max_length - len(batch)))

    if not return_word_ids:
        batched_tokens = [[(t, w) for t, w,_ in x] for x in batched_tokens]

    return batched_tokens


# from diffusers.utils import load_image
def load_image(image: Union[str, PIL.Image.Image]) -> PIL.Image.Image:
    """
    Loads `image` to a PIL Image.

    Args:
        image (`str` or `PIL.Image.Image`):
            The image to convert to the PIL Image format.
    Returns:
        `PIL.Image.Image`:
            A PIL Image.
    """
    if isinstance(image, str):
        if image.startswith("http://") or image.startswith("https://"):
            image = PIL.Image.open(requests.get(image, stream=True).raw)
        elif os.path.isfile(image):
            image = PIL.Image.open(image)
        else:
            raise ValueError(
                f"Incorrect path or url, URLs must start with `http://` or `https://`, and {image} is not a valid path"
            )
    elif isinstance(image, PIL.Image.Image):
        image = image
    else:
        raise ValueError(
            "Incorrect format used for image. Should be an url linking to an image, a local path, or a PIL image."
        )
    image = PIL.ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    return image
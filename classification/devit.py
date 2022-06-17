import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial

from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.registry import register_model
from timm.models.vision_transformer import _cfg
import math


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.dwconv = DWConv(hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        x = self.fc1(x)
        x = self.dwconv(x, H, W)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., stage=1, windowsize=7):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        self.windowsize = windowsize

        if stage < 3:
            self.mul = [1, 1]
        if stage == 3:
            self.mul = [1]

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = DEPTHWISECONV(dim,dim)

        self.unfolds = nn.ModuleList()
        self.fcs = nn.ModuleList()
        self.single_heads = nn.ModuleList()
        self.local_convs = nn.ModuleList()
        self.scales = []
        self.single_dim = []
        self.group_dim = []
        self.group_dim.append(0)
        for i_layer in range(len(self.mul)):
            dilation = 2 ** i_layer
            kernel_size = self.windowsize
            stride = dilation * (kernel_size - 1) + 1
            single_d = dim // sum(self.mul) * self.mul[i_layer]
            scale = (single_d //  self.num_heads) ** -0.5
            group_d = self.group_dim[i_layer] + single_d
            unfold = nn.Unfold(kernel_size=kernel_size, stride=stride, padding=0, dilation=dilation)
            fc = nn.Linear(single_d // self.num_heads, single_d // self.num_heads, bias=qkv_bias)
            single = nn.Linear(single_d // self.num_heads, 2 * single_d // self.num_heads, bias=qkv_bias)
            local_conv = nn.Conv2d(single_d, single_d, kernel_size=3, padding=1, stride=1, groups=single_d)
            self.unfolds.append(unfold)
            self.fcs.append(fc)
            self.single_heads.append(single)
            self.local_convs.append(local_conv)
            self.single_dim.append(single_d)
            self.scales.append(scale)
            self.group_dim.append(group_d)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        B, N, C = x.shape
        q = self.q(x).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        #B,N,C - B,N,C - B,N,h,C/h - B,h,N,C/h
        x = x.reshape(B, H, W, C).permute(0, 3, 1, 2)   #B,N,C - B,H,W,C - B,C,H,W
        kv = self.kv(x) #B,C,H,W - B,C,H,W

        attn_c = []
        for i_c in range(len(self.mul)):
            q_ = q[:,:,:,self.group_dim[i_c]//self.num_heads:self.group_dim[i_c+1]//self.num_heads]   #B,h,N,C_/h

            kv_ = self.unfolds[i_c](kv[:,self.group_dim[i_c]:self.group_dim[i_c+1],:,:]) #B,C_,H,W - B,C_*L2,H_i*W_i
            kv_ = kv_.reshape(B, self.num_heads, self.single_dim[i_c] // self.num_heads, self.windowsize**2, -1).permute(0,1,3,4,2)
            #B,C_*L2,H_i*W_i - B,h,C_/h,L2,H_i*W_i - B,h,L2,H_i*W_i,C_/h
            kv_ = kv_.reshape(B,self.num_heads,self.windowsize**2,-1) #B,h,L2,H_i*W_i,C_/h - B,h,L2,H_i*W_i*C_/h
            kv_ = nn.AdaptiveAvgPool2d((None, self.single_dim[i_c] // self.num_heads))(kv_) #B,h,L2,H_i*W_i*C_/h - B,h,L2,C_/h
            kv_ = self.fcs[i_c](kv_)  #B,h,L2,C_/h - B,h,L2,C_/h
            kv_ = self.single_heads[i_c](kv_)    #B,h,L2,C_/h - B,h,L2,2C_/h 
            k_ = kv_[:,:,:,:(self.single_dim[i_c] // self.num_heads)] #B,h,L2,C_/h
            v_ = kv_[:,:,:,(self.single_dim[i_c] // self.num_heads):]
            
            attn_ = (q_ @ k_.transpose(-2, -1)) * self.scales[i_c]  #B,h,N,C_/h - B,h,N,L2
            attn_ = attn_.softmax(dim=-1)
            attn_ = self.attn_drop(attn_)
            v_ = v_ + self.local_convs[i_c](v_.transpose(2, 3).reshape(B,self.single_dim[i_c],self.windowsize,self.windowsize)).view(B, self.num_heads, self.single_dim[i_c] // self.num_heads, self.windowsize**2).transpose(2, 3)
            #B,h,L2,C_/h - B,h,C_/h,L2 - B,C,L,L - B,C,L,L - B,h,C/h,L2 - B,h,L2,C_/h
            attn_ = (attn_ @ v_).permute(0, 2, 1, 3) #B,h,N,L2 - B,h,N,C_/h - B,N,h,C_/h
            attn_ = attn_.reshape(B, -1, self.single_dim[i_c])  #B,N,h,C_/h - B,N,C_
            attn_c.append(attn_)
        
        attn = attn_c[0]
        if len(self.mul) > 1:
            for i_c in range(1,len(self.mul)):
                attn = torch.cat((attn,attn_c[i_c]),-1)

        x = self.proj(attn)
        x = self.proj_drop(x)

        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, stage=1):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, stage=stage)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        x = x + self.drop_path(self.attn(self.norm1(x), H, W))
        x = x + self.drop_path(self.mlp(self.norm2(x), H, W))

        return x


class OverlapPatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=7, stride=4, in_chans=3, embed_dim=768):
        super().__init__()
        
        patch_size = to_2tuple(patch_size)
        
        assert max(patch_size) > stride, "Set larger patch_size than stride"
        
        self.patch_size = patch_size
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride,
                              padding=(patch_size[0] // 2, patch_size[1] // 2))
        self.norm = nn.LayerNorm(embed_dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x):
        x = self.proj(x)
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)

        return x, H, W


class DefactorizationTransformer(nn.Module):
    def __init__(self, img_size=224, patch_size=4, in_chans=3, num_classes=1000, embed_dims=[64, 128, 256, 512],
                 num_heads=[1, 2, 4, 8], mlp_ratios=[8, 6, 4, 4], qkv_bias=False, qk_scale=None, drop_rate=0.,
                 attn_drop_rate=0., drop_path_rate=0., norm_layer=nn.LayerNorm,
                 depths=[2, 2, 5, 2], num_stages=4):
        super().__init__()
        self.num_classes = num_classes
        self.depths = depths
        self.num_stages = num_stages

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0

        for i in range(num_stages):
            patch_embed = OverlapPatchEmbed(img_size=img_size if i == 0 else img_size // (2 ** (i + 1)),
                                            patch_size=7 if i == 0 else 3,
                                            stride=4 if i == 0 else 2,
                                            in_chans=in_chans if i == 0 else embed_dims[i - 1],
                                            embed_dim=embed_dims[i])

            block = nn.ModuleList([Block(
                dim=embed_dims[i], num_heads=num_heads[i], mlp_ratio=mlp_ratios[i], qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + j], norm_layer=norm_layer,
                stage=i)
                for j in range(depths[i])])
            norm = norm_layer(embed_dims[i])
            cur += depths[i]

            setattr(self, f"patch_embed{i + 1}", patch_embed)
            setattr(self, f"block{i + 1}", block)
            setattr(self, f"norm{i + 1}", norm)

        # classification head
        self.head = nn.Linear(embed_dims[3], num_classes) if num_classes > 0 else nn.Identity()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def freeze_patch_emb(self):
        self.patch_embed1.requires_grad = False

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed1', 'pos_embed2', 'pos_embed3', 'pos_embed4', 'cls_token'}  # has pos_embed may be better

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x):
        B = x.shape[0]

        for i in range(self.num_stages):
            patch_embed = getattr(self, f"patch_embed{i + 1}")
            block = getattr(self, f"block{i + 1}")
            norm = getattr(self, f"norm{i + 1}")
            x, H, W = patch_embed(x)
            for blk in block:
                x = blk(x, H, W)
            x = norm(x)
            if i != self.num_stages - 1:
                x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()

        return x.mean(dim=1)

    def forward(self, x):
        x = self.forward_features(x)
        x = self.head(x)

        return x


class DWConv(nn.Module):
    def __init__(self, dim=768):
        super(DWConv, self).__init__()
        self.dwconv = nn.Conv2d(dim, dim, 3, 1, 1, bias=True, groups=dim)

    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.transpose(1, 2).view(B, C, H, W)
        x = self.dwconv(x)
        x = x.flatten(2).transpose(1, 2)

        return x


class DEPTHWISECONV(nn.Module):
    def __init__(self,in_ch,out_ch):
        super(DEPTHWISECONV, self).__init__()
        self.depth_conv = nn.Conv2d(in_channels=in_ch,
                                    out_channels=in_ch,
                                    kernel_size=3,
                                    stride=1,
                                    padding=1,
                                    groups=in_ch)
        self.point_conv = nn.Conv2d(in_channels=in_ch,
                                    out_channels=out_ch,
                                    kernel_size=1,
                                    stride=1,
                                    padding=0,
                                    groups=1)
    def forward(self,input):
        out = self.depth_conv(input)
        out = self.point_conv(out)
        return out


def _conv_filter(state_dict, patch_size=16):
    """ convert patch embedding weight from manual patchify + linear proj to conv"""
    out_dict = {}
    for k, v in state_dict.items():
        if 'patch_embed.proj.weight' in k:
            v = v.reshape((v.shape[0], 3, patch_size, patch_size))
        out_dict[k] = v

    return out_dict


@register_model
def devit_b0(pretrained=False, **kwargs):
    model = DefactorizationTransformer(
        patch_size=4, embed_dims=[32, 64, 128, 256], num_heads=[1, 2, 4, 8], mlp_ratios=[8, 6, 4, 4], qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 2, 5, 2],
        **kwargs)
    model.default_cfg = _cfg()

    return model


@register_model
def devit_b1(pretrained=False, **kwargs):
    model = DefactorizationTransformer(
        patch_size=4, embed_dims=[64, 128, 256, 512], num_heads=[1, 2, 4, 8], mlp_ratios=[8, 6, 4, 4], qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 2, 5, 2],
        **kwargs)
    model.default_cfg = _cfg()

    return model


@register_model
def devit_b2(pretrained=False, **kwargs):
    model = DefactorizationTransformer(
        patch_size=4, embed_dims=[64, 128, 256, 512], num_heads=[1, 2, 4, 8], mlp_ratios=[8, 6, 4, 4], qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 3, 16, 3], **kwargs)
    model.default_cfg = _cfg()

    return model


@register_model
def devit2_b3(pretrained=False, **kwargs):
    model = DefactorizationTransformer(
        patch_size=4, embed_dims=[64, 128, 256, 512], num_heads=[1, 2, 4, 8], mlp_ratios=[8, 6, 4, 4], qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 4, 34, 4],
        **kwargs)
    model.default_cfg = _cfg()

    return model


@register_model
def devit_b4(pretrained=False, **kwargs):
    model = DefactorizationTransformer(
        patch_size=4, embed_dims=[64, 128, 256, 512], num_heads=[1, 2, 4, 8], mlp_ratios=[8, 6, 4, 4], qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 4, 56, 4],
        **kwargs)
    model.default_cfg = _cfg()

    return model


@register_model
def devit_b5(pretrained=False, **kwargs):
    model = DefactorizationTransformer(
        patch_size=4, embed_dims=[64, 128, 256, 512], num_heads=[1, 2, 4, 8], mlp_ratios=[4, 4, 4, 4], qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 4, 78, 4],
        **kwargs)
    model.default_cfg = _cfg()

    return model

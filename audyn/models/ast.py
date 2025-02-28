import copy
import os
import warnings
from abc import abstractmethod
from collections import OrderedDict
from typing import Dict, Optional, Tuple, Union

import torch
import torch.nn as nn
from omegaconf import OmegaConf
from torch.nn.common_types import _size_2_t

from ..modules.vit import PositionalPatchEmbedding
from ..utils.github import download_file_from_github_release

__all__ = [
    "AudioSpectrogramTransformer",
    "PositionalPatchEmbedding",  # for backward compatibility
    "Aggregator",
    "AverageAggregator",
    "HeadTokensAggregator",
    "Head",
    "MLPHead",
    "AST",
]


class BaseAudioSpectrogramTransformer(nn.Module):
    """Base class of audio spectrogram transformer."""

    def __init__(
        self,
        embedding: "PositionalPatchEmbedding",
        backbone: nn.TransformerEncoder,
    ) -> None:
        super().__init__()

        self.embedding = embedding
        self.backbone = backbone

    def patch_transformer_forward(self, input: torch.Tensor) -> torch.Tensor:
        """Transformer with patch inputs.

        Args:
            input (torch.Tensor): Patch feature of shape
                (batch_size, embedding_dim, height, width).

        Returns:
            torch.Tensor: Estimated patches of shape (batch_size, embedding_dim, height, width).

        """
        _, _, height, width = input.size()

        x = self.patches_to_sequence(input)
        x = self.transformer_forward(x)
        output = self.sequence_to_patches(x, height=height, width=width)

        return output

    def transformer_forward(self, input: torch.Tensor) -> torch.Tensor:
        output = self.backbone(input)

        return output

    def spectrogram_to_patches(self, input: torch.Tensor) -> torch.Tensor:
        """Convert spectrogram to patches.

        Actual implementation depends on ``self.embedding.spectrogram_to_patches``.

        """
        return self.embedding.spectrogram_to_patches(input)

    def patches_to_sequence(self, input: Union[torch.Tensor, torch.BoolTensor]) -> torch.Tensor:
        """Convert 3D (batch_size, height, width) or 4D (batch_size, embedding_dim, height, width)
        tensor to shape (batch_size, length, *) for input of Transformer.

        Args:
            input (torch.Tensor): Patches of shape (batch_size, height, width) or
                (batch_size, embedding_dim, height, width).

        Returns:
            torch.Tensor: Sequence of shape (batch_size, length) or
                (batch_size, length, embedding_dim).

        """
        n_dims = input.dim()

        if n_dims == 3:
            batch_size, height, width = input.size()
            output = input.view(batch_size, height * width)
        elif n_dims == 4:
            batch_size, embedding_dim, height, width = input.size()
            x = input.view(batch_size, embedding_dim, height * width)
            output = x.permute(0, 2, 1).contiguous()
        else:
            raise ValueError("Only 3D and 4D tensors are supported.")

        return output

    def sequence_to_patches(
        self, input: Union[torch.Tensor, torch.BoolTensor], height: int, width: int
    ) -> torch.Tensor:
        """Convert (batch_size, max_length, *) tensor to 3D (batch_size, height, width)
        or 4D (batch_size, embedding_dim, height, width) one.
        This method corresponds to inversion of ``patches_to_sequence``.
        """
        n_dims = input.dim()

        if n_dims == 2:
            batch_size, _ = input.size()
            output = input.view(batch_size, height, width)
        elif n_dims == 3:
            batch_size, _, embedding_dim = input.size()
            x = input.view(batch_size, height, width, embedding_dim)
            output = x.permute(0, 3, 1, 2).contiguous()
        else:
            raise ValueError("Only 2D and 3D tensors are supported.")

        return output

    def split_sequence(self, sequence: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """Split sequence to head tokens and content tokens.

        Args:
            sequence (torch.Tensor): Sequence containing head tokens, i.e. class and distillation
                tokens. The shape is (batch_size, length, embedding_dim).

        Returns:
            tuple: Tuple of tensors containing

                - torch.Tensor: Head tokens of shape (batch_size, num_head_tokens, embedding_dim).
                - torch.Tensor: Sequence of shape
                    (batch_size, length - num_head_tokens, embedding_dim).

        .. note::

            This method is applicable even when sequence does not contain head tokens. In that
            case, an empty sequnce is returened as the first item of returned tensors.

        """
        head_tokens, sequence = self.embedding.split_sequence(sequence)

        return head_tokens, sequence

    def prepend_tokens(
        self, sequence: torch.Tensor, tokens: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Prepaned tokens to sequence.

        Args:
            sequence (torch.Tensor): Sequence of shape (batch_size, length, embedding_dim).
            tokens (torch.Tensor, optional): Tokens of shape
                (batch_size, num_tokens, embedding_dim).

        Returns:
            torch.Tensor: Concatenated sequence of shape
                (batch_size, length + num_tokens, embedding_dim).

        """
        if tokens is None:
            return sequence
        else:
            return torch.cat([tokens, sequence], dim=-2)


class AudioSpectrogramTransformer(BaseAudioSpectrogramTransformer):
    """Audio spectrogram transformer.

    Args:
        embedding (audyn.models.ast.PositionalPatchEmbedding): Patch embedding
            followed by positional embedding.
        backbone (nn.TransformerEncoder): Transformer (encoder).

    """

    def __init__(
        self,
        embedding: "PositionalPatchEmbedding",
        backbone: nn.TransformerEncoder,
        aggregator: Optional["Aggregator"] = None,
        head: Optional["Head"] = None,
    ) -> None:
        super().__init__(embedding=embedding, backbone=backbone)

        self.aggregator = aggregator
        self.head = head

        if self.aggregator is None and self.head is not None:
            warnings.warn(
                "Head is given, but aggregator is not given, "
                "which may lead to unexpected behavior.",
                UserWarning,
                stacklevel=2,
            )

    @classmethod
    def build_from_pretrained(
        cls,
        pretrained_model_name_or_path: str,
        stride: Optional[_size_2_t] = None,
        n_bins: Optional[int] = None,
        n_frames: Optional[int] = None,
        aggregator: Optional[nn.Module] = None,
        head: Optional[nn.Module] = None,
    ) -> "AudioSpectrogramTransformer":
        """Build pretrained AudioSpectrogramTransformer.

        Args:
            pretrained_model_name_or_path (str): Path to pretrained model or name of pretrained model.
            aggregator (nn.Module, optional): Aggregator module.
            head (nn.Module, optional): Head module.

        Examples:

            >>> from audyn.models.ast import AudioSpectrogramTransformer
            >>> model = AudioSpectrogramTransformer.build_from_pretrained("ast-base-stride10")

        .. note::

            Supported pretrained model names are
                - ast-base-stride10

        """  # noqa: E501
        from ..utils.hydra.utils import instantiate  # to avoid circular import

        pretrained_model_configs = _create_pretrained_model_configs()

        if os.path.exists(pretrained_model_name_or_path):
            state_dict = torch.load(
                pretrained_model_name_or_path, map_location=lambda storage, loc: storage
            )
            model_state_dict: OrderedDict = state_dict["model"]
            resolved_config = state_dict["resolved_config"]
            resolved_config = OmegaConf.create(resolved_config)
            pretrained_model_config = resolved_config.model
            model: AudioSpectrogramTransformer = instantiate(pretrained_model_config)
            model.load_state_dict(model_state_dict)

            if aggregator is not None:
                model.aggregator = aggregator

            if head is not None:
                model.head = head

            # update patch embedding if necessary
            model.embedding = _align_patch_embedding(
                model.embedding, stride=stride, n_bins=n_bins, n_frames=n_frames
            )

            return model
        elif pretrained_model_name_or_path in pretrained_model_configs:
            config = pretrained_model_configs[pretrained_model_name_or_path]
            url = config["url"]
            path = config["path"]
            download_file_from_github_release(url, path=path)
            model = cls.build_from_pretrained(
                path,
                stride=stride,
                n_bins=n_bins,
                n_frames=n_frames,
                aggregator=aggregator,
                head=head,
            )

            return model
        else:
            raise FileNotFoundError(f"{pretrained_model_name_or_path} does not exist.")

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        """Forward pass of AudioSpectrogramTransformer.

        Args:
            input (torch.Tensor): Spectrogram of shape (batch_size, n_bins, n_frames).

        Returns:
            torch.Tensor: Estimated patches. The shape is one of
                - (batch_size, height * width + num_head_tokens, embedding_dim).
                - (batch_size, height * width + num_head_tokens, out_channels).
                - (batch_size, embedding_dim).
                - (batch_size, out_channels).

        """
        x = self.embedding(input)
        output = self.transformer_forward(x)

        if self.aggregator is not None:
            output = self.aggregator(output)

        if self.head is not None:
            output = self.head(output)

        return output


class Aggregator(nn.Module):
    @abstractmethod
    def forward(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
    ) -> torch.Tensor:
        """Forward pass of Aggregator.

        Args:
            input (torch.Tensor): Sequence of shape (batch_size, length, embedding_dim).
            padding_mask (torch.BoolTensor, optional): Padding mask of shape (batch_size, length).

        Returns:
            torch.Tensor: Aggregated feature of shape (batch_size, embedding_dim).

        """
        pass


class AverageAggregator(Aggregator):
    def __init__(self, insert_cls_token: bool = True, insert_dist_token: bool = True) -> None:
        super().__init__()

        if not insert_cls_token and not insert_dist_token:
            raise ValueError(
                "At least one of insert_cls_token and insert_dist_token should be True."
            )

        self.insert_cls_token = insert_cls_token
        self.insert_dist_token = insert_dist_token

    def forward(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
    ) -> torch.Tensor:
        """Forward pass of AverageAggregator.

        Args:
            input (torch.Tensor): Sequence of shape (batch_size, length, embedding_dim).
            padding_mask (torch.BoolTensor, optional): Padding mask of shape (batch_size, length).

        Returns:
            torch.Tensor: Aggregated feature of shape (batch_size, embedding_dim).

        """
        num_head_tokens = 0

        if self.insert_cls_token:
            num_head_tokens += 1

        if self.insert_dist_token:
            num_head_tokens += 1

        _, x = torch.split(input, [num_head_tokens, input.size(-2) - num_head_tokens], dim=-2)

        if padding_mask is None:
            batch_size, length, _ = x.size()
            padding_mask = torch.full(
                (batch_size, length),
                fill_value=False,
                dtype=torch.bool,
                device=x.device,
            )

        x = x.masked_fill(padding_mask.unsqueeze(dim=-1), 0)
        non_padding_mask = torch.logical_not(padding_mask)
        non_padding_mask = non_padding_mask.to(torch.long)
        output = x.sum(dim=-2) / non_padding_mask.sum(dim=-1, keepdim=True)

        return output


class HeadTokensAggregator(Aggregator):
    def __init__(self, insert_cls_token: bool = True, insert_dist_token: bool = True) -> None:
        super().__init__()

        if not insert_cls_token and not insert_dist_token:
            raise ValueError(
                "At least one of insert_cls_token and insert_dist_token should be True."
            )

        self.insert_cls_token = insert_cls_token
        self.insert_dist_token = insert_dist_token

    def forward(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
    ) -> torch.Tensor:
        """Forward pass of HeadTokensAggregator.

        Args:
            input (torch.Tensor): Sequence of shape (batch_size, length, embedding_dim).
            padding_mask (torch.BoolTensor, optional): Padding mask of shape (batch_size, length).

        Returns:
            torch.Tensor: Aggregated feature of shape (batch_size, embedding_dim).

        .. note::

            padding_mask is ignored.

        """
        num_head_tokens = 0

        if self.insert_cls_token:
            num_head_tokens += 1

        if self.insert_dist_token:
            num_head_tokens += 1

        head_tokens, _ = torch.split(
            input, [num_head_tokens, input.size(-2) - num_head_tokens], dim=-2
        )
        output = torch.mean(head_tokens, dim=-2)

        return output


class Head(nn.Module):
    @abstractmethod
    def forward(self, input: torch.Tensor) -> torch.Tensor:
        pass


class MLPHead(Head):
    def __init__(self, in_channels: int, out_channels: int) -> None:
        super().__init__()

        self.norm = nn.LayerNorm(in_channels)
        self.linear = nn.Linear(in_channels, out_channels)

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        """Forward pass of MLPHead.

        Args:
            input (torch.Tensor): Aggregated feature of shape (batch_size, in_channels).

        Returns:
            torch.Tensor: Transformed feature of shape (batch_size, out_channels).

        """
        x = self.norm(input)
        output = self.linear(x)

        return output


class AST(AudioSpectrogramTransformer):
    """Alias of AudioSpectrogramTransformer."""


def _create_pretrained_model_configs() -> Dict[str, Dict[str, str]]:
    """Create pretrained_model_configs without circular import error."""

    from ..utils import model_cache_dir

    pretrained_model_configs = {
        "ast-base-stride10": {
            "url": "https://github.com/tky823/Audyn/releases/download/v0.0.1.dev3/ast-base-stride10.pth",  # noqa: E501
            "path": os.path.join(
                model_cache_dir,
                "AudioSpectrogramTransformer",
                "ast-base-stride10.pth",
            ),
        },
    }

    return pretrained_model_configs


def _align_patch_embedding(
    orig_patch_embedding: PositionalPatchEmbedding,
    stride: Optional[_size_2_t] = None,
    n_bins: Optional[int] = None,
    n_frames: Optional[int] = None,
) -> PositionalPatchEmbedding:
    pretrained_embedding_dim = orig_patch_embedding.embedding_dim
    pretrained_kernel_size = orig_patch_embedding.kernel_size
    pretrained_stride = orig_patch_embedding.stride
    pretrained_insert_cls_token = orig_patch_embedding.insert_cls_token
    pretrained_insert_dist_token = orig_patch_embedding.insert_dist_token
    pretrained_n_bins = orig_patch_embedding.n_bins
    pretrained_n_frames = orig_patch_embedding.n_frames
    pretrained_conv2d = orig_patch_embedding.conv2d
    pretrained_positional_embedding = orig_patch_embedding.positional_embedding
    pretrained_cls_token = orig_patch_embedding.cls_token
    pretrained_dist_token = orig_patch_embedding.dist_token

    if stride is None:
        stride = pretrained_stride

    if n_bins is None:
        n_bins = pretrained_n_bins

    if n_frames is None:
        n_frames = pretrained_n_frames

    new_patch_embedding = PositionalPatchEmbedding(
        pretrained_embedding_dim,
        kernel_size=pretrained_kernel_size,
        stride=stride,
        insert_cls_token=pretrained_insert_cls_token,
        insert_dist_token=pretrained_insert_dist_token,
        n_bins=n_bins,
        n_frames=n_frames,
    )

    conv2d_state_dict = copy.deepcopy(pretrained_conv2d.state_dict())
    new_patch_embedding.conv2d.load_state_dict(conv2d_state_dict)

    pretrained_positional_embedding = new_patch_embedding.resample_positional_embedding(
        pretrained_positional_embedding, n_bins, n_frames
    )
    new_patch_embedding.positional_embedding.data.copy_(pretrained_positional_embedding)

    if pretrained_insert_cls_token:
        new_patch_embedding.cls_token.data.copy_(pretrained_cls_token)

    if pretrained_insert_dist_token:
        new_patch_embedding.dist_token.data.copy_(pretrained_dist_token)

    return new_patch_embedding

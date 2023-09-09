from typing import Optional, Tuple, Union

import torch

from .glow import ActNorm1d

__all__ = ["MaskedActNorm1d"]


class MaskedActNorm1d(ActNorm1d):
    """ActNorm1d with padding mask.

    This module takes variable-length input.
    """

    def _initialize_parameters(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
    ) -> None:
        if padding_mask is None:
            super()._initialize_parameters(input)
        else:
            expanded_padding_mask = self._expand_padding_mask(input, padding_mask)
            expanded_non_padding_mask = torch.logical_not(expanded_padding_mask)
            num_elements = expanded_non_padding_mask.sum(dim=(0, 2))
            masked_input = input.masked_fill(expanded_padding_mask, 0)
            mean = masked_input.sum(dim=(0, 2)) / num_elements

            zero_mean_input = masked_input - mean.unsqueeze(dim=-1)
            squared_input = torch.masked_fill(zero_mean_input**2, expanded_padding_mask, 0)
            log_std = 0.5 * (torch.log(squared_input.sum(dim=(0, 2))) - torch.log(num_elements))

            self.log_std.data.copy_(log_std)
            self.mean.data.copy_(mean)

            self.is_initialized = True

    def _forward(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
        logdet: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if padding_mask is None:
            output, logdet = super()._forward(input, logdet=logdet)
        else:
            expanded_padding_mask = self._expand_padding_mask(input, padding_mask)
            expanded_non_padding_mask = torch.logical_not(expanded_padding_mask)
            # count elements per batch dimension
            num_elements = expanded_non_padding_mask.sum(dim=(1, 2))

            log_std = self.log_std.unsqueeze(dim=-1)
            std = torch.exp(log_std)
            mean = self.mean.unsqueeze(dim=-1)
            x = (input - mean) / std
            output = x.masked_fill(expanded_padding_mask, 0)

            if logdet is not None:
                log_std = log_std.expand(input.size())
                log_std = log_std.masked_fill(expanded_padding_mask, 0)
                logdet = logdet - num_elements * log_std.sum(dim=(1, 2))

        return output, logdet

    def _reverse(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
        logdet: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if padding_mask is None:
            output, logdet = super()._reverse(input, logdet=logdet)
        else:
            expanded_padding_mask = self._expand_padding_mask(input, padding_mask)
            expanded_non_padding_mask = torch.logical_not(expanded_padding_mask)
            # count elements per batch dimension
            num_elements = expanded_non_padding_mask.sum(dim=(1, 2))

            log_std = self.log_std.unsqueeze(dim=-1)
            std = torch.exp(log_std)
            mean = self.mean.unsqueeze(dim=-1)
            x = std * input + mean
            output = x.masked_fill(expanded_padding_mask, 0)

            if logdet is not None:
                log_std = log_std.expand(input.size())
                log_std = log_std.masked_fill(expanded_padding_mask, 0)
                logdet = logdet + num_elements * log_std.sum(dim=(1, 2))

        return output, logdet

    @staticmethod
    def _expand_padding_mask(
        input: torch.Tensor,
        padding_mask: torch.BoolTensor,
    ) -> torch.BoolTensor:
        """Expand padding mask.

        Args:
            input (torch.Tensor): Tensor of shape (batch_size, num_features, length).
            padding_mask (torch.BoolTensor): Padding mask of shape
                (batch_size, length) or (batch_size, num_features, length).

        Returns:
            torch.BoolTensor: Expanded padding mask of shape (batch_size, num_features, length).

        """
        if padding_mask.dim() == 2:
            padding_mask = padding_mask.unsqueeze(dim=1)
        elif padding_mask.dim() != 3:
            raise ValueError(f"{padding_mask.dim()}D mask is not supported.")

        expanded_padding_mask = padding_mask.expand(input.size())

        return expanded_padding_mask

    def forward(
        self,
        input: torch.Tensor,
        padding_mask: Optional[torch.BoolTensor] = None,
        logdet: Optional[torch.Tensor] = None,
        reverse: bool = False,
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """Forward pass of MaskedActNorm1d.

        Args:
            input (torch.Tensor): Tensor of shape (batch_size, num_features, length).
            padding_mask (torch.BoolTensor): Padding mask of shape
                (batch_size, length) or (batch_size, num_features, length).

        Returns:
            torch.Tensor: Transformed tensor of same shape as input.

        """
        if self.training and not self.is_initialized:
            self._initialize_parameters(input, padding_mask=padding_mask)

        logdet = self.initialize_logdet_if_necessary(logdet, device=input.device)

        if reverse:
            output, logdet = self._reverse(
                input,
                padding_mask=padding_mask,
                logdet=logdet,
            )
        else:
            output, logdet = self._forward(
                input,
                padding_mask=padding_mask,
                logdet=logdet,
            )

        if logdet is None:
            return output
        else:
            return output, logdet

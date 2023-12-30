"""This file is based on
https://github.com/pytorch/pytorch/blob/0093df78df590a35deb784773aa2165884c1b7bd/torch/optim/optimizer.py.
"""
import copy
import math
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Type, Union, overload

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from packaging import version
from torch.optim import Optimizer
from torch.utils.hooks import RemovableHandle

from ..modules.rvq import ResidualVectorQuantizer
from ..modules.vq import VectorQuantizer

__all__ = [
    "ExponentialMovingAverageWrapper",
    "ExponentialMovingAverageCodebookOptimizer",
    "GANOptimizer",
]

IS_TORCH_LT_2_1 = version.parse(torch.__version__) < version.parse("2.1")


class MovingAverageWrapper(Optimizer):
    """Wrapper class of optimizer to perform moving average of parameters.

    Args:
        optimizer (Optimizer): Optimizer to update model parameters.
        smooth (float): Smoothing factor. Default: ``0.999``.

    Examples:

            >>> import torch
            >>> import torch.nn as nn
            >>> from torch.optim import Adam
            >>> from audyn.optim.optimizer import MovingAverageWrapper
            >>> in_channels, out_channels = 3, 2
            >>> lr = 1e-3
            >>> smooth = 0.999
            >>> model = nn.Linear(in_channels, out_channels)
            >>> optimizer = Adam(model.parameters(), lr=lr)
            >>> optimizer = MovingAverageWrapper(optimizer, smooth=smooth)
            >>> # or you can instantiate by build_from_optim_class
            >>> optimizer = MovingAverageWrapper.build_from_optim_class(
            ...     model.parameters(), optimizer_class=Adam, lr=lr, smooth=smooth
            ... )

    """

    def __init__(self, optimizer: Optimizer, smooth: float = 0.999) -> None:
        self.optimizer = optimizer
        self.smooth = smooth

        self.moving_average_param_groups = [
            {"params": copy.deepcopy(param_group["params"])}
            for param_group in self.optimizer.param_groups
        ]
        self.cached_param_groups = None

    @classmethod
    def build_from_optim_class(
        cls, *args, optimizer_class: Type, smooth: float = 0.999, **kwargs
    ) -> "MovingAverageWrapper":
        """Build moving average wrapper of specified optimizer.

        Args:
            optimizer_class (type): Optimizer class.
            smooth (float): Smoothing factor. Default: ``0.999``.
            args: Positional arguments given to instantiation of optimizer.
            kwargs: Keyword arguments given to instantiation of optimizer.

        Returns:
            MovingAverageWrapper: Moving average wrapper of optimizer.

        Examples:

            >>> import torch
            >>> import torch.nn as nn
            >>> from torch.optim import Adam
            >>> from audyn.optim.optimizer import MovingAverageWrapper
            >>> in_channels, out_channels = 3, 2
            >>> lr = 1e-3
            >>> smooth = 0.999
            >>> model = nn.Linear(in_channels, out_channels)
            >>> optimizer = MovingAverageWrapper.build_from_optim_class(
            ...     model.parameters(), optimizer_class=Adam, lr=lr, smooth=smooth
            ... )

        """
        optimizer = optimizer_class(*args, **kwargs)

        return cls(optimizer, smooth=smooth)

    def step(self) -> None:
        """Performs a single optimization step and update moving average."""
        raise NotImplementedError("`step` is not implemented.")

    def state_dict(self) -> Dict[str, Any]:
        """Returns the state of the optimizer as a ``dict``.

        Returns:
            dict: State dict of optimizer and moving average parameters.

        """
        state_dict = {}

        param_mappings = {}
        start_index = 0

        def _pack_param_group(param_group):
            nonlocal start_index
            param_mappings.update(
                {
                    id(p): i
                    for i, p in enumerate(param_group["params"], start_index)
                    if id(p) not in param_mappings
                }
            )
            packed = {"params": [param_mappings[id(p)] for p in param_group["params"]]}
            start_index += len(packed["params"])

            return packed

        param_groups = [
            _pack_param_group(param_group) for param_group in self.moving_average_param_groups
        ]
        packed_state = {}

        for group in self.moving_average_param_groups:
            for k, params in group.items():
                assert k == "params", "Only params is supported."

                for p in params:
                    assert isinstance(
                        p, torch.Tensor
                    ), "Only torch.Tensor is supported, but found {}.".format(type(p))

                    packed_state.update({param_mappings[id(p)]: p.data})

        packed_state["smooth"] = self.smooth

        moving_averate_state_dict = {
            "state": packed_state,
            "param_groups": param_groups,
        }
        state_dict["moving_average"] = moving_averate_state_dict
        state_dict["original"] = self.optimizer.state_dict()

        return state_dict

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        r"""Loads the optimizer state.

        Args:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to ``state_dict``.

        """
        moving_averate_state_dict = state_dict["moving_average"]
        optimizer_state_dict = state_dict["original"]

        # deepcopy, to be consistent with module API
        moving_averate_state_dict = copy.deepcopy(moving_averate_state_dict)
        # Validate the state_dict
        groups = self.moving_average_param_groups
        saved_packed_groups = moving_averate_state_dict["param_groups"]

        if len(groups) != len(saved_packed_groups):
            raise ValueError("Loaded state dict has a different number of parameter groups.")

        param_lens = (len(group["params"]) for group in groups)
        saved_lens = (len(group["params"]) for group in saved_packed_groups)

        if any(p_len != s_len for p_len, s_len in zip(param_lens, saved_lens)):
            raise ValueError(
                "Loaded state dict contains a parameter group "
                "that doesn't match the size of optimizer's group."
            )

        param_mappings = {}
        start_index = 0

        for group in groups:
            params = group["params"]
            param_mappings.update(
                {
                    id(p): i
                    for i, p in enumerate(params, start_index)
                    if id(p) not in param_mappings
                }
            )
            packed_params = []

            for p in params:
                param_id = param_mappings[id(p)]
                packed_params.append(param_id)
                p.data = moving_averate_state_dict["state"][param_id]

            start_index += len(packed_params)

        # for backward compatibility
        self.smooth = moving_averate_state_dict["state"].get("smooth", 0.999)

        # Load state dict of optimizer
        self.optimizer.load_state_dict(optimizer_state_dict)

    def set_moving_average_model(self) -> None:
        """Set moving averaged parameters to model."""
        if self.cached_param_groups is not None:
            raise ValueError("Call remove_moving_average_model before.")

        self.cached_param_groups = copy.deepcopy(self.optimizer.param_groups)

        for param_group, param_group_moving_average in zip(
            self.optimizer.param_groups, self.moving_average_param_groups
        ):
            for p, p_moving_average in zip(
                param_group["params"], param_group_moving_average["params"]
            ):
                p.data = p_moving_average.data

    def remove_moving_average_model(self) -> None:
        """Set original parameters to model."""
        if self.cached_param_groups is None:
            raise ValueError("Call set_moving_average_model before.")

        for param_group, cache_param_group in zip(
            self.optimizer.param_groups, self.cached_param_groups
        ):
            for p, p_cache in zip(param_group["params"], cache_param_group["params"]):
                p.data = p_cache.data

        self.cached_param_groups = None

    # define methods as those of self.optimizer
    def __getstate__(self) -> Dict[str, Any]:
        return self.optimizer.__getstate__()

    def __setstate__(self, state: Dict[str, Any]) -> None:
        return self.optimizer.__setstate__(state)

    def __repr__(self) -> str:
        return self.optimizer.__repr__()

    def _cuda_graph_capture_health_check(self) -> None:
        return self.optimizer._cuda_graph_capture_health_check()

    def _optimizer_step_code(self) -> None:
        return self.optimizer._optimizer_step_code()

    def _patch_step_function(self) -> None:
        return self.optimizer._patch_step_function()

    def register_step_pre_hook(self, hook: Callable[[Any], None]) -> RemovableHandle:
        return self.optimizer.register_step_pre_hook(hook)

    def register_step_post_hook(self, hook: Callable[[Any], None]) -> RemovableHandle:
        return self.optimizer.register_step_post_hook(hook)

    def add_param_group(self, param_group: Dict[str, Any]) -> None:
        return self.optimizer.add_param_group(param_group)

    # if not found __name as attribute of self, search self.optimizer instead.
    def __getattr__(self, __name: str) -> Any:
        return getattr(self.optimizer, __name)


class ExponentialMovingAverageWrapper(MovingAverageWrapper):
    """Wrapper class of optimizer to perform exponential moving average of parameters.

    Args:
        optimizer (Optimizer): Optimizer to update model parameters.
        smooth (float): Smoothing factor. Default: ``0.999``.

    Examples:

        >>> import torch
        >>> import torch.nn as nn
        >>> from torch.optim import Adam
        >>> from audyn.optim.optimizer import ExponentialMovingAverageWrapper
        >>> torch.manual_seed(0)
        >>> in_channels, out_channels = 3, 2
        >>> lr = 1e-3
        >>> smooth = 0.999
        >>> model = nn.Linear(in_channels, out_channels)
        >>> optimizer = Adam(model.parameters(), lr=lr)
        >>> optimizer = ExponentialMovingAverageWrapper(optimizer, smooth=smooth)
        >>> criterion = nn.MSELoss()
        >>> input, target = torch.randn(in_channels), torch.randn(out_channels)
        >>> output = model(input)
        >>> loss = criterion(output, target)
        >>> optimizer.zero_grad()
        >>> loss.backward()
        >>> optimizer.step()
        >>> model.weight
        Parameter containing:
        tensor([[-0.0053,  0.3087, -0.4742],
                [-0.4239, -0.2214,  0.1538]], requires_grad=True)
        >>> optimizer.set_moving_average_model()
        >>> model.weight
        Parameter containing:
        tensor([[-0.0043,  0.3097, -0.4752],
                [-0.4249, -0.2224,  0.1548]], requires_grad=True)
        >>> optimizer.remove_moving_average_model()
        >>> model.weight
        Parameter containing:
        tensor([[-0.0053,  0.3087, -0.4742],
                [-0.4239, -0.2214,  0.1538]], requires_grad=True)

    """

    def __init__(self, optimizer: Optimizer, smooth: float = 0.999) -> None:
        super().__init__(optimizer, smooth)

    def step(self, *args, **kwargs) -> None:
        """Performs a single optimization step and update exponential moving average."""
        self.optimizer.step(*args, **kwargs)

        for param_group, moving_average_param_group in zip(
            self.optimizer.param_groups, self.moving_average_param_groups
        ):
            for p, p_moving_average in zip(
                param_group["params"], moving_average_param_group["params"]
            ):
                p_moving_average.data = torch.lerp(
                    p.data, p_moving_average.data, weight=self.smooth
                )


class _ExponentialMovingAverageCodebookOptimizer(Optimizer):
    if IS_TORCH_LT_2_1:

        @overload
        def __init__(
            self,
            params: Iterable,
            smooth: float = 0.999,
            reset_step: Optional[int] = None,
            reset_var: Optional[float] = None,
            reset_rate: Optional[float] = None,
            seed: int = 0,
        ) -> None:
            ...

    else:
        from torch.optim.optimizer import params_t

        @overload
        def __init__(
            self,
            params: params_t,
            smooth: float = 0.999,
            reset_step: Optional[int] = None,
            reset_var: Optional[float] = None,
            reset_rate: Optional[float] = None,
            seed: int = 0,
        ) -> None:
            ...

    def __init__(
        self,
        params,
        smooth=0.999,
        reset_step=None,
        reset_var=None,
        reset_rate=None,
        seed=0,
    ) -> None:
        defaults = {}
        params = self._trim_scalar_parameters(params)

        super().__init__(params, defaults)

        if reset_step is None:
            if reset_var is not None:
                raise ValueError("reset_var is specified, but reset_step is not defined.")

            if reset_rate is not None:
                raise ValueError("reset_rate is specified, but reset_step is not defined.")

            codebook_reset = False
        else:
            codebook_reset = True

        if codebook_reset:
            accumulated_steps = 0

            if reset_var is None:
                reset_var = 0.01

            if reset_rate is None:
                reset_rate = 0.03
        else:
            accumulated_steps = None

        self.smooth = smooth

        # running stats
        self.num_samples_tracked_groups: List[List[torch.Tensor]]
        self.momentum_groups: List[List[torch.Tensor]]

        # current stats
        self.one_hot_sum_groups: List[List[torch.Tensor]]
        self.z_e_sum_groups: List[List[torch.Tensor]]

        # codebook reset
        self.num_accumulated_groups: Optional[List[List[torch.LongTensor]]]
        self.codebook_reset = codebook_reset
        self.accumulated_steps = accumulated_steps
        self.reset_step = reset_step
        self.reset_var = reset_var
        self.reset_rate = reset_rate

        # for DDP and codebook reset
        self.seed = seed
        self.iteration = 0

    def state_dict(self) -> Dict[str, Any]:
        """Returns the state of the optimizer as a ``dict``.

        Returns:
            dict: State dict of optimizer and moving average parameters.

        """
        state_dict = {}

        param_groups, param_mappings = _pack_param_groups(self.param_groups)
        num_samples_tracked_groups, num_samples_tracked_mappings = _pack_groups(
            self.num_samples_tracked_groups
        )
        momentum_groups, momentum_mappings = _pack_groups(self.momentum_groups)
        one_hot_sum_groups, one_hot_sum_mappings = _pack_groups(self.one_hot_sum_groups)
        z_e_sum_groups, z_e_sum_mappings = _pack_groups(self.z_e_sum_groups)

        packed_param_state = _pack_param_state(self.param_groups, param_mappings)
        packed_num_samples_tracked_state = _pack_state(
            self.num_samples_tracked_groups, num_samples_tracked_mappings
        )
        packed_momentum_state = _pack_state(self.momentum_groups, momentum_mappings)
        packed_one_hot_sum_state = _pack_state(self.one_hot_sum_groups, one_hot_sum_mappings)
        packed_z_e_sum_state = _pack_state(self.z_e_sum_groups, z_e_sum_mappings)

        state_dict = {
            "param_state": packed_param_state,
            "param_groups": param_groups,
            "num_samples_tracked_state": packed_num_samples_tracked_state,
            "num_samples_tracked_groups": num_samples_tracked_groups,
            "momentum_state": packed_momentum_state,
            "momentum_groups": momentum_groups,
            "one_hot_sum_state": packed_one_hot_sum_state,
            "one_hot_sum_groups": one_hot_sum_groups,
            "z_e_sum_state": packed_z_e_sum_state,
            "z_e_sum_groups": z_e_sum_groups,
            "smooth": self.smooth,
        }

        # Though seed and iteration are used only for codebook reset,
        # we always save them.
        state_dict.update(
            {
                "seed": self.seed,
                "iteration": self.iteration,
            }
        )

        if self.codebook_reset:
            num_accumulated_groups, num_accumulated_mappings = _pack_groups(
                self.num_accumulated_groups
            )
            packed_num_accumulated_state = _pack_state(
                self.num_accumulated_groups, num_accumulated_mappings
            )

            state_dict.update(
                {
                    "num_accumulated_state": packed_num_accumulated_state,
                    "num_accumulated_groups": num_accumulated_groups,
                    "accumulated_steps": self.accumulated_steps,
                    "reset_step": self.reset_step,
                    "reset_var": self.reset_var,
                    "reset_rate": self.reset_rate,
                }
            )

        return state_dict

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        r"""Loads the optimizer state.

        Args:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to ``state_dict``.

        """

        def _validate_groups(groups, saved_groups, keys: Optional[List[str]] = None) -> None:
            if len(groups) != len(saved_groups):
                raise ValueError("Loaded state dict has a different number of parameter groups.")

            if keys is None:
                containing_lens = (len(group) for group in groups)
                saved_lens = (len(saved_group) for saved_group in saved_groups)

                if any(c_len != s_len for c_len, s_len in zip(containing_lens, saved_lens)):
                    raise ValueError(
                        "Loaded state dict contains a parameter group "
                        "that doesn't match the size of optimizer's group."
                    )
            else:
                for k in keys:
                    containing_lens = (len(group[k]) for group in groups)
                    saved_lens = (len(saved_group[k]) for saved_group in saved_groups)

                    if any(c_len != s_len for c_len, s_len in zip(containing_lens, saved_lens)):
                        raise ValueError(
                            "Loaded state dict contains a parameter group "
                            "that doesn't match the size of optimizer's group."
                        )

        # deepcopy, to be consistent with module API
        state_dict = copy.deepcopy(state_dict)

        param_groups = self.param_groups
        saved_packed_param_groups = state_dict["param_groups"]
        packed_param_state = state_dict["param_state"]

        num_samples_tracked_groups = self.num_samples_tracked_groups
        saved_packed_num_samples_tracked_groups = state_dict["num_samples_tracked_groups"]
        packed_num_samples_tracked_state = state_dict["num_samples_tracked_state"]
        momentum_groups = self.momentum_groups
        saved_packed_momentum_groups = state_dict["momentum_groups"]
        packed_momentum_state = state_dict["momentum_state"]
        one_hot_sum_groups = self.one_hot_sum_groups
        saved_packed_one_hot_sum_groups = state_dict["one_hot_sum_groups"]
        packed_one_hot_sum_state = state_dict["one_hot_sum_state"]
        z_e_sum_groups = self.z_e_sum_groups
        saved_packed_z_e_sum_groups = state_dict["z_e_sum_groups"]
        packed_z_e_sum_state = state_dict["z_e_sum_state"]

        # validate state_dict
        _validate_groups(param_groups, saved_packed_param_groups, keys=["params"])
        _validate_groups(num_samples_tracked_groups, saved_packed_num_samples_tracked_groups)
        _validate_groups(momentum_groups, saved_packed_momentum_groups)
        _validate_groups(one_hot_sum_groups, saved_packed_one_hot_sum_groups)
        _validate_groups(z_e_sum_groups, saved_packed_z_e_sum_groups)

        _load_param_groups(param_groups, packed_param_state)
        _load_groups(num_samples_tracked_groups, packed_num_samples_tracked_state)
        _load_groups(momentum_groups, packed_momentum_state)
        _load_groups(one_hot_sum_groups, packed_one_hot_sum_state)
        _load_groups(z_e_sum_groups, packed_z_e_sum_state)

        # In older version, smooth parameter is not saved.
        self.smooth = state_dict.get("smooth", self.smooth)

        # In older version, seed and iteration are not saved.
        self.seed = state_dict.get("seed", self.seed)
        self.iteration = state_dict.get("iteration", self.iteration)

        if self.codebook_reset:
            num_accumulated_groups = self.num_accumulated_groups
            saved_packed_num_samples_tracked_groups = state_dict["num_accumulated_groups"]
            packed_num_accumulated_state = state_dict["num_accumulated_state"]

            _validate_groups(num_accumulated_groups, saved_packed_num_samples_tracked_groups)
            _load_groups(num_accumulated_groups, packed_num_accumulated_state)

            self.accumulated_steps = state_dict["accumulated_steps"]
            self.reset_step = state_dict["reset_step"]
            self.reset_var = state_dict["reset_var"]
            self.reset_rate = state_dict["reset_rate"]

    def _trim_scalar_parameters(self, params: Iterable) -> List[nn.Parameter]:
        fixed_params = []

        for param in params:
            if param.dim() == 0:
                # flags related to initialization
                pass
            else:
                fixed_params.append(param)

        return fixed_params


class ExponentialMovingAverageCodebookOptimizer(_ExponentialMovingAverageCodebookOptimizer):
    """Optimizer to update codebook using exponential moving average.

    Args:
        params: Parameters to be optimized.
        smooth (float): Smoothing factor. Default: ``0.999``.
        reset_step (int, optional): Step to reset codebook proposed by
            [#williams2020hierarchical]_. Default: ``None`` (Codebook
            reset is deactivated).
        reset_var (float, optional): This parameter is activated if ``reset_step``
            is specified. Variance of codebook reset. If ``None``, 0.01 is used by default.
        reset_rate (float, optional): This parameter is activated if ``reset_step``
            is specified. If usage of least used codebook is
            less than ``reset_rate``, position will be reset. If ``None``,
            0.03 is used by default.
        seed (int): Seed to synchronize states among devices when DDP is used.

    .. note::

        This class does not use gradient descent.

    .. note::

        This class supports distributed data parallel.

    .. warning::

        This class does not support data parallel.

    Examples:

        >>> import torch
        >>> from audyn.modules.vqvae import VectorQuantizer
        >>> from audyn.optim.optimizer import ExponentialMovingAverageCodebookOptimizer
        >>> torch.manual_seed(0)
        >>> codebook_size, embedding_dim = 3, 4
        >>> batch_size, length = 2, 5
        >>> model = VectorQuantizer(codebook_size, embedding_dim)
        >>> # w/o codebook reset
        >>> optimizer = ExponentialMovingAverageCodebookOptimizer(model.parameters())
        >>> model.register_forward_hook(optimizer.store_current_stats)
        >>> model.codebook.weight
        Parameter containing:
        tensor([[ 1.5410, -0.2934, -2.1788,  0.5684],
                [-1.0845, -1.3986,  0.4033,  0.8380],
                [-0.7193, -0.4033, -0.5966,  0.1820]], requires_grad=True)
        >>> input = torch.randn((batch_size, embedding_dim, length))
        >>> output, indices = model(input)
        >>> optimizer.step()
        >>> model.codebook.weight
        Parameter containing:
        tensor([[ 1.4807, -0.1351, -2.1238,  0.6231],
                [-1.0845, -1.3986,  0.4033,  0.8380],
                [-0.2900,  0.1278, -0.2841, -0.0953]], requires_grad=True)

        >>> import torch
        >>> from audyn.modules.rvq import ResidualVectorQuantizer
        >>> from audyn.optim.optimizer import ExponentialMovingAverageCodebookOptimizer
        >>> torch.manual_seed(0)
        >>> num_stages, codebook_size, embedding_dim = 6, 3, 4
        >>> batch_size, length = 2, 5
        >>> model = ResidualVectorQuantizer(codebook_size, embedding_dim, num_stages=num_stages)
        >>> # w/o codebook reset
        >>> optimizer = ExponentialMovingAverageCodebookOptimizer(model.parameters())
        >>> model.register_forward_hook(optimizer.store_current_stats)
        >>> model.codebooks[0].weight
        Parameter containing:
        tensor([[ 1.5410, -0.2934, -2.1788,  0.5684],
                [-1.0845, -1.3986,  0.4033,  0.8380],
                [-0.7193, -0.4033, -0.5966,  0.1820]], requires_grad=True)
        >>> input = torch.randn((batch_size, embedding_dim, length))
        >>> output, indices = model(input)
        >>> optimizer.step()
        >>> model.codebooks[0].weight
        Parameter containing:
        tensor([[ 1.5410, -0.2934, -2.1788,  0.5684],
                [-1.0824, -1.3987,  0.4036,  0.8382],
                [-0.7094, -0.4002, -0.5933,  0.1820]], requires_grad=True)

    .. [#williams2020hierarchical]
        W. Williams et al., "Hierarchical quantized autoencoders,"
        in *NeurIPS*, 2020, pp.4524-4535,

    """

    if IS_TORCH_LT_2_1:

        @overload
        def __init__(
            self,
            params: Iterable,
            smooth: float = 0.999,
            reset_step: Optional[int] = None,
            reset_var: Optional[float] = None,
            reset_rate: Optional[float] = None,
        ) -> None:
            ...

    else:
        from torch.optim.optimizer import params_t

        @overload
        def __init__(
            self,
            params: params_t,
            smooth: float = 0.999,
            reset_step: Optional[int] = None,
            reset_var: Optional[float] = None,
            reset_rate: Optional[float] = None,
        ) -> None:
            ...

    def __init__(
        self,
        params,
        smooth=0.999,
        reset_step=None,
        reset_var=None,
        reset_rate=None,
    ) -> None:
        super().__init__(
            params,
            smooth=smooth,
            reset_step=reset_step,
            reset_var=reset_var,
            reset_rate=reset_rate,
        )

        # running stats
        num_samples_tracked_groups = []
        momentum_groups = []

        # current stats
        one_hot_sum_groups = []
        z_e_sum_groups = []

        # codebook reset
        num_accumulated_groups = []

        for param_group in self.param_groups:
            num_samples_tracked = []
            momentum = []
            one_hot_sum_group = []
            z_e_sum_group = []
            num_accumulated = []

            for param in param_group["params"]:
                weight: torch.Tensor = param.data
                device = weight.device

                # We assume each codebook is used at least once,
                # which is helpful to avoid zero division in updates of codebooks.
                _num_samples_tracked = torch.ones(
                    weight.size(0), device=device, dtype=weight.dtype
                )
                num_samples_tracked.append(_num_samples_tracked)

                _momentum = torch.empty_like(weight)
                _momentum.data.copy_(weight.data)
                momentum.append(_momentum)

                one_hot_sum = torch.zeros(weight.size(0), device=device, dtype=torch.long)
                one_hot_sum_group.append(one_hot_sum)
                z_e_sum = torch.zeros_like(weight)
                z_e_sum_group.append(z_e_sum)

                if self.codebook_reset:
                    _num_accumulated = torch.zeros(weight.size(0), device=device, dtype=torch.long)
                else:
                    _num_accumulated = None

                num_accumulated.append(_num_accumulated)

            num_samples_tracked_groups.append(num_samples_tracked)
            momentum_groups.append(momentum)
            one_hot_sum_groups.append(one_hot_sum_group)
            z_e_sum_groups.append(z_e_sum_group)
            num_accumulated_groups.append(num_accumulated)

        self.num_samples_tracked_groups = num_samples_tracked_groups
        self.momentum_groups = momentum_groups
        self.one_hot_sum_groups = one_hot_sum_groups
        self.z_e_sum_groups = z_e_sum_groups

        self.num_accumulated_groups = num_accumulated_groups

    def store_current_stats(
        self, module: Union[VectorQuantizer, ResidualVectorQuantizer], input: Any, output: Any
    ) -> None:
        # TODO: generalize
        if isinstance(module, VectorQuantizer):
            is_rvq = False
        elif isinstance(module, ResidualVectorQuantizer):
            is_rvq = True
        else:
            raise ValueError("Only VectorQuantizer and ResidualVectorQuantizer are supported.")

        codebook_reset = self.codebook_reset
        is_distributed = dist.is_available() and dist.is_initialized()

        param_groups = self._trim_scalar_parameters(module.parameters())
        tracking_param_groups = self.param_groups
        one_hot_sum_groups = self.one_hot_sum_groups
        z_e_sum_groups = self.z_e_sum_groups
        num_accumulated_groups = self.num_accumulated_groups

        if not isinstance(param_groups[0], dict):
            param_groups = [{"params": param_groups}]

        (dequantized_input,) = input
        _, indices = output

        if is_rvq:
            # indices: (batch_size, num_stages, embedding_dim, *)
            stacked_indices = indices.transpose(1, 0)
        else:
            # indices: (batch_size, embedding_dim, *)
            stacked_indices = indices.unsqueeze(dim=0)

        if len(param_groups) != len(tracking_param_groups):
            # param_groups should be identical to tracking_param_groups
            raise ValueError("Given parameter groups do not match tracking ones.")

        with torch.cuda.amp.autocast(enabled=False):
            for (
                indices,
                param_group,
                tracking_param_group,
                one_hot_sum_group,
                z_e_sum_group,
                num_accumulated,
            ) in zip(
                stacked_indices,
                param_groups,
                tracking_param_groups,
                one_hot_sum_groups,
                z_e_sum_groups,
                num_accumulated_groups,
            ):
                if len(param_group["params"]) != len(tracking_param_group["params"]):
                    # param_group["params"] should be identical to tracking_param_group["params"]
                    raise ValueError("Given parameters do not match tracking ones.")

                for idx, param in enumerate(param_group["params"]):
                    codebook_size, embedding_dim = param.data.size()

                    dequantized_input = dequantized_input.transpose(1, 0).contiguous()
                    dequantized_input = dequantized_input.view(embedding_dim, -1)
                    one_hot = F.one_hot(indices, num_classes=codebook_size)
                    one_hot = one_hot.view(-1, codebook_size)

                    if is_distributed:
                        # gather dequantized_input and one_hot
                        # dequantized_input:
                        #     (embedding_dim, num_samples)
                        #     -> (embedding_dim, num_gpus * num_samples)
                        # one_hot:
                        #     (num_samples, codebook_size)
                        #     -> (num_gpus * num_samples, codebook_size)
                        gathered_dequantized_input = [
                            torch.zeros_like(dequantized_input)
                            for _ in range(dist.get_world_size())
                        ]
                        gathered_one_hot = [
                            torch.zeros_like(one_hot) for _ in range(dist.get_world_size())
                        ]

                        dist.all_gather(gathered_dequantized_input, dequantized_input)
                        dist.all_gather(gathered_one_hot, one_hot)

                        dequantized_input = torch.cat(gathered_dequantized_input, dim=1)
                        one_hot = torch.cat(gathered_one_hot, dim=0)

                    one_hot_sum = one_hot.sum(dim=0)
                    one_hot = one_hot.to(dequantized_input.dtype)

                    # NOTE: In some cases with mixed precision training,
                    #       the following matmul operation may cause inf.
                    z_e_sum = torch.matmul(dequantized_input, one_hot)
                    z_e_sum = z_e_sum.permute(1, 0).contiguous()

                    one_hot_sum_group[idx].data.copy_(one_hot_sum.data)
                    z_e_sum_group[idx].data.copy_(z_e_sum.data)

                    if codebook_reset:
                        num_accumulated[idx].data.add_(one_hot_sum.data)

    def step(self) -> None:
        param_groups = self.param_groups

        num_samples_tracked_groups = self.num_samples_tracked_groups
        momentum_groups = self.momentum_groups
        one_hot_sum_groups = self.one_hot_sum_groups
        z_e_sum_groups = self.z_e_sum_groups
        num_accumulated_groups = self.num_accumulated_groups

        smooth = self.smooth
        codebook_reset = self.codebook_reset

        self.iteration += 1

        if codebook_reset:
            self.accumulated_steps += 1

        for (
            param_group,
            num_samples_tracked_group,
            momentum_group,
            one_hot_sum_group,
            z_e_sum_group,
            num_accumulated_group,
        ) in zip(
            param_groups,
            num_samples_tracked_groups,
            momentum_groups,
            one_hot_sum_groups,
            z_e_sum_groups,
            num_accumulated_groups,
        ):
            for (
                param,
                num_samples_tracked,
                momentum,
                one_hot_sum,
                z_e_sum,
                num_accumulated,
            ) in zip(
                param_group["params"],
                num_samples_tracked_group,
                momentum_group,
                one_hot_sum_group,
                z_e_sum_group,
                num_accumulated_group,
            ):
                param: nn.Parameter
                one_hot_sum_data = one_hot_sum.data.to(num_samples_tracked.data.dtype)
                num_samples_tracked.data = torch.lerp(
                    one_hot_sum_data,
                    num_samples_tracked.data,
                    weight=smooth,
                )
                momentum.data = torch.lerp(
                    z_e_sum.data,
                    momentum.data,
                    weight=smooth,
                )
                param.data = momentum / num_samples_tracked.unsqueeze(dim=-1)

                if codebook_reset and self.accumulated_steps % self.reset_step == 0:
                    std = math.sqrt(self.reset_var)

                    least_usage, min_idx = torch.min(num_accumulated, dim=0)
                    most_usage, max_idx = torch.max(num_accumulated, dim=0)

                    if least_usage < self.reset_rate * most_usage:
                        # to ensure synchronization in DDP, use random number generator
                        most_used = param.data[max_idx]
                        g = torch.Generator(device=most_used.device)
                        g.manual_seed(self.seed + self.iteration)
                        replaced = most_used + std * torch.randn(
                            most_used.size(),
                            generator=g,
                            device=most_used.device,
                            dtype=most_used.dtype,
                        )
                        param.data[min_idx].copy_(replaced)

                        # reset statistics
                        momentum.data[min_idx].copy_(replaced)
                        num_samples_tracked.data[min_idx].fill_(1)
                        num_accumulated.data.zero_()


class MultiOptimizers:
    """Module to manage multiple optimizers.

    .. note::

        To use this class with learning scheduler, you have to choose MultiLRSchedulers.

    """

    # TODO: improve design

    def __init__(self, optimizers: List[Union[Dict[str, Any], Optimizer]]) -> None:
        self.optimizers = {}

        for idx, optimizer in enumerate(optimizers):
            if isinstance(optimizer, Optimizer):
                k = str(idx)
                v = optimizer
            elif isinstance(optimizer, dict):
                k = optimizer["name"]
                v = optimizer["optimizer"]
            else:
                raise ValueError(f"{type(optimizer)} is not supported.")

            if k in self.optimizers.keys():
                raise ValueError(f"Duplicate optimizer name {k} is found.")

            self.optimizers[k] = v

    def zero_grad(self, *args, **kwargs) -> None:
        for optimizer in self.optimizers.values():
            optimizer: Optimizer
            optimizer.zero_grad(*args, **kwargs)

    def step(self, *args, **kwargs) -> None:
        for optimizer in self.optimizers.values():
            optimizer: Optimizer
            optimizer.step(*args, **kwargs)

    def state_dict(self, *args, **kwargs) -> Dict[str, Dict[str, Any]]:
        state_dict = {}

        for name, optimizer in self.optimizers.items():
            optimizer: Optimizer
            state_dict[name] = optimizer.state_dict(*args, **kwargs)

        return state_dict

    def load_state_dict(self, state_dict: Dict[str, Dict[str, Any]]) -> None:
        r"""Loads the optimizer state.

        Args:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to ``state_dict``.

        """

        for name, optimizer in self.optimizers.items():
            optimizer: Optimizer
            optimizer.load_state_dict(state_dict[name])


class GANOptimizer:
    def __init__(self, generator: Optimizer, discriminator: Optimizer) -> None:
        self.generator = generator
        self.discriminator = discriminator

    def zero_grad(self, *args, **kwargs) -> None:
        self.generator.zero_grad(*args, **kwargs)
        self.discriminator.zero_grad(*args, **kwargs)

    def step(self, *args, **kwargs) -> None:
        self.generator.step(*args, **kwargs)
        self.discriminator.step(*args, **kwargs)


# pack
def _pack_param_groups(
    param_groups: Dict[str, List[Any]]
) -> Tuple[List[Dict[str, Any]], Dict[int, int]]:
    param_mappings = {}
    start_index = 0

    def _pack_param_group(param_group):
        nonlocal start_index
        param_mappings.update(
            {
                id(p): i
                for i, p in enumerate(param_group["params"], start_index)
                if id(p) not in param_mappings
            }
        )
        packed = {"params": [param_mappings[id(p)] for p in param_group["params"]]}
        start_index += len(packed["params"])

        return packed

    param_groups = [_pack_param_group(param_group) for param_group in param_groups]

    return param_groups, param_mappings


def _pack_param_state(
    param_groups: List[Dict[str, List[Any]]], param_mappings: Dict[int, int]
) -> Dict[int, Any]:
    # Remap state to use order indices as keys
    packed_state = {}

    for group in param_groups:
        for k, v in group.items():
            assert k == "params", "Only params is supported."

            for p in v:
                assert isinstance(
                    p, torch.Tensor
                ), "Only torch.Tensor is supported, but found {}.".format(type(p))

                packed_state.update({param_mappings[id(p)]: p.data})

    return packed_state


def _pack_groups(groups: List[Any]) -> Tuple[List[Any], Dict[int, int]]:
    # for ExponentialMovingAverageCodebookOptimizer
    mappings = {}
    start_index = 0

    def _pack_group(group):
        nonlocal start_index
        mappings.update(
            {id(p): i for i, p in enumerate(group, start_index) if id(p) not in mappings}
        )
        packed = [mappings[id(p)] for p in group]
        start_index += len(packed)

        return packed

    groups = [_pack_group(group) for group in groups]

    return groups, mappings


def _pack_state(groups: List[Any], mappings: Dict[int, int]) -> Dict[int, Any]:
    # Remap state to use order indices as keys
    packed_state = {}

    for group in groups:
        for p in group:
            assert isinstance(
                p, torch.Tensor
            ), "Only torch.Tensor is supported, but found {}.".format(type(p))

            packed_state.update({mappings[id(p)]: p.data})

    return packed_state


# load
def _load_param_groups(
    param_groups: List[Dict[str, Any]], packed_param_state: Dict[int, Any]
) -> None:
    param_mappings = {}
    start_index = 0

    for group in param_groups:
        params = group["params"]
        param_mappings.update(
            {id(p): i for i, p in enumerate(params, start_index) if id(p) not in param_mappings}
        )
        packed_params = []

        for p in params:
            p: nn.Parameter
            param_id = param_mappings[id(p)]
            packed_params.append(param_id)
            p.data = packed_param_state[param_id]

        start_index += len(packed_params)


def _load_groups(param_groups: List[Dict[str, Any]], packed_param_state: Dict[int, Any]) -> None:
    param_mappings = {}
    start_index = 0

    for group in param_groups:
        params = group
        param_mappings.update(
            {id(p): i for i, p in enumerate(params, start_index) if id(p) not in param_mappings}
        )
        packed_params = []

        for p in params:
            p: nn.Parameter
            param_id = param_mappings[id(p)]
            packed_params.append(param_id)
            p.data = packed_param_state[param_id]

        start_index += len(packed_params)

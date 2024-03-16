import importlib
import os
import sys
import tempfile
from datetime import timedelta
from os.path import dirname, join, realpath, relpath
from typing import Any, Dict, List, Optional, Tuple

import hydra
import pytest
import torch
import torch.distributed as dist
import torch.nn as nn
import webdataset as wds
from dummy import allclose
from omegaconf import DictConfig, OmegaConf
from pytest import MonkeyPatch

import audyn
from audyn.criterion.gan import GANCriterion
from audyn.models.gan import BaseGAN
from audyn.optim.lr_scheduler import GANLRScheduler
from audyn.optim.optimizer import GANOptimizer
from audyn.utils import (
    convert_dataloader_to_ddp_if_possible,
    instantiate,
    instantiate_cascade_text_to_wave,
    instantiate_criterion,
    instantiate_grad_clipper,
    instantiate_lr_scheduler,
    instantiate_model,
    instantiate_optimizer,
    setup_system,
)
from audyn.utils.clip_grad import GANGradClipper
from audyn.utils.data import BaseDataLoaders, TorchObjectDataset, default_collate_fn, make_noise
from audyn.utils.data.dataset import WebDatasetWrapper
from audyn.utils.driver import (
    BaseGenerator,
    BaseTrainer,
    CascadeTextToWaveGenerator,
    FeatToWaveTrainer,
    GANTrainer,
    TextToFeatTrainer,
)
from audyn.utils.model import set_device

IS_WINDOWS = sys.platform == "win32"

config_template_path = join(dirname(realpath(audyn.__file__)), "utils", "driver", "_conf_template")
config_name = "config"


@pytest.mark.parametrize("use_ema", [True, False])
def test_base_drivers(monkeypatch: MonkeyPatch, use_ema: bool) -> None:
    """Test BaseTrainer and BaseGenerator."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    INITIAL_ITERATION = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        train_name = "dummy"
        model_name = "dummy"
        criterion_name = "dummy"
        lr_scheduler_name = "dummy"

        if use_ema:
            optimizer_name = "dummy"
        else:
            optimizer_name = "dummy_ema"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=INITIAL_ITERATION,
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                    optimizer=optimizer_name,
                    lr_scheduler=lr_scheduler_name,
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)
        test_dataset = instantiate(config.test.dataset.test)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
        )
        test_loader = instantiate(
            config.test.dataloader.test,
            test_dataset,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = BaseTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        target_grad = torch.tensor(
            list(
                range(BATCH_SIZE * INITIAL_ITERATION - BATCH_SIZE, BATCH_SIZE * INITIAL_ITERATION)
            ),
            dtype=torch.float,
        )
        target_grad = -torch.mean(target_grad)

        allclose(model.linear.weight.grad.data, target_grad)

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=len(train_loader),
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                    optimizer=optimizer_name,
                    lr_scheduler=lr_scheduler_name,
                    continue_from=f"{exp_dir}/model/iteration{INITIAL_ITERATION}.pth",
                ),
                return_hydra_config=True,
            )

        trainer = BaseTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        target_grad = torch.tensor(
            list(range(DATA_SIZE - BATCH_SIZE, DATA_SIZE)), dtype=torch.float
        )
        target_grad = -torch.mean(target_grad)

        allclose(model.linear.weight.grad.data, target_grad)

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    checkpoint=f"{exp_dir}/model/last.pth",
                ),
                return_hydra_config=True,
            )

        model = instantiate_model(config.test.checkpoint)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        generator = BaseGenerator(
            test_loader,
            model,
            config=config,
        )
        generator.run()

        monkeypatch.undo()


def test_base_trainer_ddp(monkeypatch: MonkeyPatch) -> None:
    """Test BaseTrainer for DDP."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        # set environmental variables
        os.environ["LOCAL_RANK"] = "0"
        os.environ["RANK"] = "0"
        os.environ["WORLD_SIZE"] = "1"
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = str(torch.randint(0, 2**16, ()).item())

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            system_name = "cpu_ddp"
            train_name = "dummy"
            model_name = "dummy"
            criterion_name = "dummy"
            lr_scheduler_name = "dummy"
            optimizer_name = "dummy"

            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    system=system_name,
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                    optimizer=optimizer_name,
                    lr_scheduler=lr_scheduler_name,
                ),
                return_hydra_config=True,
            )

        assert config.system.distributed.enable
        assert config.train.dataloader.train._target_ == "torch.utils.data.DataLoader"

        convert_dataloader_to_ddp_if_possible(config)

        dist.init_process_group(
            backend=config.system.distributed.backend,
            init_method=config.system.distributed.init_method,
            rank=int(os.environ["RANK"]),
            world_size=int(os.environ["WORLD_SIZE"]),
            timeout=timedelta(minutes=1),
        )
        torch.manual_seed(config.system.seed)

        assert config.system.distributed.enable
        assert (
            config.train.dataloader.train._target_
            == "audyn.utils.data.dataloader.DistributedDataLoader"
        )

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)
        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = nn.parallel.DistributedDataParallel(model)
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)

        trainer = BaseTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        dist.destroy_process_group()

        monkeypatch.undo()


@pytest.mark.parametrize("model_name", ["dummy_text-to-feat", "dummy_glowtts"])
@pytest.mark.parametrize(
    "is_legacy_grad_clipper, is_legacy_grad_clipper_recipe",
    [(True, True), (True, False), (False, False)],
)
def test_text_to_feat_trainer(
    monkeypatch: MonkeyPatch,
    model_name: str,
    is_legacy_grad_clipper: bool,
    is_legacy_grad_clipper_recipe: bool,
) -> None:
    """Test TextToFeatTrainer."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3
    VOCAB_SIZE = 10
    N_MELS = 5
    TEXT_TO_FEAT_UP_SCALE = 2

    if model_name == "dummy_glowtts":
        train_name = "dummy_glowtts"
    else:
        train_name = "dummy_text-to-feat"

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_text_to_feat_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    vocab_size=VOCAB_SIZE,
                    n_mels=N_MELS,
                    up_scale=TEXT_TO_FEAT_UP_SCALE,
                    train=train_name,
                    model=model_name,
                    pretrained_feat_to_wave=None,
                ),
                return_hydra_config=True,
            )

        if not is_legacy_grad_clipper:
            assert config.train.clip_gradient._target_ == "torch.nn.utils.clip_grad_norm_"

            config = OmegaConf.to_container(config)
            config["train"]["clip_gradient"].update(
                {
                    "_target_": "audyn.utils.GradClipper",
                    "mode": "norm",
                }
            )
            config = OmegaConf.create(config)

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)

        if is_legacy_grad_clipper and is_legacy_grad_clipper_recipe:
            grad_clipper = None
        else:
            grad_clipper = instantiate_grad_clipper(config.train.clip_gradient, model.parameters())

        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = TextToFeatTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


def test_text_to_feat_with_pretrained_feat_to_wave_trainer(monkeypatch: MonkeyPatch) -> None:
    """Test TextToFeatTrainer."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3
    VOCAB_SIZE = 10
    N_MELS = 5
    TEXT_TO_FEAT_UP_SCALE = 2

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        # feat-to-wave
        feat_to_wave_exp_dir = os.path.join(exp_dir, "feat-to-wave")

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_feat_to_wave_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=feat_to_wave_exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    n_mels=N_MELS,
                    up_scale=TEXT_TO_FEAT_UP_SCALE,
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = FeatToWaveTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        # text-to-feat
        text_to_feat_exp_dir = os.path.join(exp_dir, "text-to-feat")
        pretrained_feat_to_wave = os.path.join(feat_to_wave_exp_dir, "model", "last.pth")

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_text_to_feat_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=text_to_feat_exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    vocab_size=VOCAB_SIZE,
                    n_mels=N_MELS,
                    up_scale=TEXT_TO_FEAT_UP_SCALE,
                    pretrained_feat_to_wave=pretrained_feat_to_wave,
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)

        if "clip_gradient" in config.train:
            grad_clipper = instantiate_grad_clipper(config.train.clip_gradient, model.parameters())
        else:
            grad_clipper = None

        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = TextToFeatTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


@pytest.mark.parametrize("use_ema", [True, False])
@pytest.mark.parametrize("use_lr_scheduler", [True, False])
@pytest.mark.parametrize(
    "is_legacy_grad_clipper, is_legacy_grad_clipper_recipe",
    [(True, True), (True, False), (False, False)],
)
def test_feat_to_wave_trainer(
    monkeypatch: MonkeyPatch,
    use_ema: bool,
    use_lr_scheduler: bool,
    is_legacy_grad_clipper: bool,
    is_legacy_grad_clipper_recipe: bool,
):
    """Test FeatToWaveTrainer."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    INITIAL_ITERATION = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        train_name = "dummy_autoregressive_feat_to_wave"
        model_name = "dummy_autoregressive"
        criterion_name = "dummy_autoregressive"

        if use_ema:
            optimizer_name = "dummy"
        else:
            optimizer_name = "dummy_ema"

        if use_lr_scheduler:
            lr_scheduler_name = "dummy"
        else:
            lr_scheduler_name = "none"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=INITIAL_ITERATION,
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                    optimizer=optimizer_name,
                    lr_scheduler=lr_scheduler_name,
                ),
                return_hydra_config=True,
            )

        if not is_legacy_grad_clipper:
            assert config.train.clip_gradient._target_ == "torch.nn.utils.clip_grad_norm_"

            config = OmegaConf.to_container(config)
            config["train"]["clip_gradient"].update(
                {
                    "_target_": "audyn.utils.GradClipper",
                    "mode": "norm",
                }
            )
            config = OmegaConf.create(config)

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=pad_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=pad_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)

        if is_legacy_grad_clipper and is_legacy_grad_clipper_recipe:
            grad_clipper = None
        else:
            grad_clipper = instantiate_grad_clipper(config.train.clip_gradient, model.parameters())

        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = FeatToWaveTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


@pytest.mark.parametrize("use_ema_generator", [True, False])
@pytest.mark.parametrize("use_ema_discriminator", [True, False])
@pytest.mark.parametrize(
    "is_legacy_grad_clipper, is_legacy_grad_clipper_recipe",
    [(True, True), (True, False), (False, False)],
)
def test_gan_trainer(
    monkeypatch: MonkeyPatch,
    use_ema_generator: bool,
    use_ema_discriminator: bool,
    is_legacy_grad_clipper: bool,
    is_legacy_grad_clipper_recipe: bool,
):
    DATA_SIZE = 20
    BATCH_SIZE = 2
    INITIAL_ITERATION = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        train_name = "dummy_gan"
        model_name = "dummy_gan"
        criterion_name = "dummy_gan"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_gan_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=INITIAL_ITERATION,
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                    use_ema_generator=use_ema_generator,
                    use_ema_discriminator=use_ema_discriminator,
                ),
                return_hydra_config=True,
            )

        if is_legacy_grad_clipper:
            if not is_legacy_grad_clipper_recipe:
                assert config.train.clip_gradient._target_ == "torch.nn.utils.clip_grad_norm_"

                max_norm = config.train.clip_gradient.max_norm
                config = OmegaConf.to_container(config)
                config["train"]["clip_gradient"].update(
                    {
                        "generator": {
                            "_target_": "torch.nn.utils.clip_grad_norm_",
                            "max_norm": max_norm,
                        },
                        "discriminator": "${.generator}",
                    }
                )

                config = OmegaConf.create(config)
        else:
            assert config.train.clip_gradient._target_ == "torch.nn.utils.clip_grad_norm_"

            max_norm = config.train.clip_gradient.max_norm
            config = OmegaConf.to_container(config)
            config["train"]["clip_gradient"].update(
                {
                    "generator": {
                        "_target_": "audyn.utils.GradClipper",
                        "mode": "norm",
                        "max_norm": max_norm,
                    },
                    "discriminator": "${.generator}",
                }
            )
            config = OmegaConf.create(config)

        setup_system(config)

        train_dataset = instantiate(
            config.train.dataset.train,
        )
        validation_dataset = instantiate(
            config.train.dataset.validation,
        )

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=gan_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=gan_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        generator = instantiate_model(config.model.generator)
        discriminator = instantiate_model(config.model.discriminator)
        generator = set_device(
            generator,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        discriminator = set_device(
            discriminator,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        generator_optimizer = instantiate_optimizer(
            config.optimizer.generator, generator.parameters()
        )
        discriminator_optimizer = instantiate_optimizer(
            config.optimizer.discriminator, discriminator.parameters()
        )
        generator_lr_scheduler = instantiate_lr_scheduler(
            config.lr_scheduler.generator, generator_optimizer
        )
        discriminator_lr_scheduler = instantiate_lr_scheduler(
            config.lr_scheduler.discriminator, discriminator_optimizer
        )

        if is_legacy_grad_clipper and is_legacy_grad_clipper_recipe:
            grad_clipper = None
        else:
            generator_grad_clipper = instantiate_grad_clipper(
                config.train.clip_gradient.generator, generator.parameters()
            )
            discriminator_grad_clipper = instantiate_grad_clipper(
                config.train.clip_gradient.discriminator, discriminator.parameters()
            )
            grad_clipper = GANGradClipper(generator_grad_clipper, discriminator_grad_clipper)

        generator_criterion = instantiate_criterion(config.criterion.generator)
        discriminator_criterion = instantiate_criterion(config.criterion.discriminator)
        generator_criterion = set_device(
            generator_criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        discriminator_criterion = set_device(
            discriminator_criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        model = BaseGAN(generator, discriminator)
        optimizer = GANOptimizer(generator_optimizer, discriminator_optimizer)
        lr_scheduler = GANLRScheduler(generator_lr_scheduler, discriminator_lr_scheduler)
        criterion = GANCriterion(generator_criterion, discriminator_criterion)

        trainer = GANTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


@pytest.mark.parametrize("train_name", ["dummy_gan", "dummy_gan_ddp"])
@pytest.mark.parametrize("dataloader_type", ["torch", "audyn_sequential"])
def test_gan_trainer_ddp(monkeypatch: MonkeyPatch, train_name: str, dataloader_type: str) -> None:
    DATA_SIZE = 20
    BATCH_SIZE = 2
    INITIAL_ITERATION = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        # set environmental variables
        os.environ["LOCAL_RANK"] = "0"
        os.environ["RANK"] = "0"
        os.environ["WORLD_SIZE"] = "1"
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = str(torch.randint(0, 2**16, ()).item())

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        model_name = "dummy_gan"
        criterion_name = "dummy_gan"

        system_name = "cpu_ddp"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_gan_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=INITIAL_ITERATION,
                    system=system_name,
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                ),
                return_hydra_config=True,
            )

        config = OmegaConf.to_container(config)
        config["train"].update(
            {
                "clip_gradient": "none",
            }
        )

        if dataloader_type == "audyn_sequential":
            if train_name == "dummy_gan":
                config["train"]["dataloader"]["train"].update(
                    {
                        "_target_": "audyn.utils.data.dataloader.SequentialBatchDataLoader",
                    }
                )
            else:
                config["train"]["dataloader"]["train"].update(
                    {
                        "_target_": "audyn.utils.data.dataloader."
                        "DistributedSequentialBatchDataLoader",
                    }
                )

        config = OmegaConf.create(config)

        assert config.system.distributed.enable

        if dataloader_type == "audyn_sequential":
            if train_name == "dummy_gan":
                assert (
                    config.train.dataloader.train._target_
                    == "audyn.utils.data.dataloader.SequentialBatchDataLoader"
                )
            else:
                assert (
                    config.train.dataloader.train._target_
                    == "audyn.utils.data.dataloader.DistributedSequentialBatchDataLoader"
                )
        else:
            if train_name == "dummy_gan":
                assert config.train.dataloader.train._target_ == "torch.utils.data.DataLoader"
            else:
                assert (
                    config.train.dataloader.train._target_
                    == "audyn.utils.data.dataloader.DistributedDataLoader"
                )

        convert_dataloader_to_ddp_if_possible(config)

        if dataloader_type == "audyn_sequential":
            assert (
                config.train.dataloader.train._target_
                == "audyn.utils.data.dataloader.DistributedSequentialBatchDataLoader"
            )
        else:
            assert (
                config.train.dataloader.train._target_
                == "audyn.utils.data.dataloader.DistributedDataLoader"
            )

        dist.init_process_group(
            backend=config.system.distributed.backend,
            init_method=config.system.distributed.init_method,
            rank=int(os.environ["RANK"]),
            world_size=int(os.environ["WORLD_SIZE"]),
            timeout=timedelta(minutes=1),
        )
        torch.manual_seed(config.system.seed)

        train_dataset = instantiate(
            config.train.dataset.train,
        )
        validation_dataset = instantiate(
            config.train.dataset.validation,
        )

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=gan_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=gan_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        generator = instantiate_model(config.model.generator)
        discriminator = instantiate_model(config.model.discriminator)
        generator = nn.parallel.DistributedDataParallel(generator)
        discriminator = nn.parallel.DistributedDataParallel(discriminator)

        generator_optimizer = instantiate_optimizer(
            config.optimizer.generator, generator.parameters()
        )
        discriminator_optimizer = instantiate_optimizer(
            config.optimizer.discriminator, discriminator.parameters()
        )
        generator_lr_scheduler = instantiate_lr_scheduler(
            config.lr_scheduler.generator, generator_optimizer
        )
        discriminator_lr_scheduler = instantiate_lr_scheduler(
            config.lr_scheduler.discriminator, discriminator_optimizer
        )
        grad_clipper = None
        generator_criterion = instantiate_criterion(config.criterion.generator)
        discriminator_criterion = instantiate_criterion(config.criterion.discriminator)

        model = BaseGAN(generator, discriminator)
        optimizer = GANOptimizer(generator_optimizer, discriminator_optimizer)
        lr_scheduler = GANLRScheduler(generator_lr_scheduler, discriminator_lr_scheduler)
        criterion = GANCriterion(generator_criterion, discriminator_criterion)

        trainer = GANTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        dist.destroy_process_group()

        system_name = "cpu"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_gan_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=INITIAL_ITERATION,
                    system=system_name,
                    train=train_name,
                    model=model_name,
                    criterion=criterion_name,
                ),
                return_hydra_config=True,
            )

        config = OmegaConf.to_container(config)
        config["train"].update(
            {
                "resume": {
                    "continue_from": os.path.join(exp_dir, "model/last.pth"),
                },
                "clip_gradient": "none",
            }
        )
        config = OmegaConf.create(config)

        assert not config.system.distributed.enable

        torch.manual_seed(config.system.seed)

        train_dataset = instantiate(
            config.train.dataset.train,
        )
        validation_dataset = instantiate(
            config.train.dataset.validation,
        )

        generator = instantiate_model(config.model.generator)
        discriminator = instantiate_model(config.model.discriminator)

        generator_optimizer = instantiate_optimizer(
            config.optimizer.generator, generator.parameters()
        )
        discriminator_optimizer = instantiate_optimizer(
            config.optimizer.discriminator, discriminator.parameters()
        )
        generator_lr_scheduler = instantiate_lr_scheduler(
            config.lr_scheduler.generator, generator_optimizer
        )
        discriminator_lr_scheduler = instantiate_lr_scheduler(
            config.lr_scheduler.discriminator, discriminator_optimizer
        )
        grad_clipper = None
        generator_criterion = instantiate_criterion(config.criterion.generator)
        discriminator_criterion = instantiate_criterion(config.criterion.discriminator)

        model = BaseGAN(generator, discriminator)
        optimizer = GANOptimizer(generator_optimizer, discriminator_optimizer)
        lr_scheduler = GANLRScheduler(generator_lr_scheduler, discriminator_lr_scheduler)
        criterion = GANCriterion(generator_criterion, discriminator_criterion)

        trainer = GANTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


def test_cascade_text_to_wave(monkeypatch: MonkeyPatch) -> None:
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3
    VOCAB_SIZE = 10
    N_MELS = 5
    TEXT_TO_FEAT_UP_SCALE = 2
    FEAT_TO_WAVE_UP_SCALE = 2

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        text_to_feat_exp_dir = "./exp/text-to-feat"
        feat_to_wave_exp_dir = "./exp/feat-to-wave"
        text_to_wave_exp_dir = "./exp/text-to-wave"

        # text-to-feat
        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_text_to_feat_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=text_to_feat_exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    vocab_size=VOCAB_SIZE,
                    n_mels=N_MELS,
                    up_scale=TEXT_TO_FEAT_UP_SCALE,
                    pretrained_feat_to_wave=None,
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)

        if "clip_gradient" in config.train:
            grad_clipper = instantiate_grad_clipper(config.train.clip_gradient, model.parameters())
        else:
            grad_clipper = None

        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = TextToFeatTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            grad_clipper=grad_clipper,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        # feat-to-wave
        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_feat_to_wave_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=feat_to_wave_exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    n_mels=N_MELS,
                    up_scale=FEAT_TO_WAVE_UP_SCALE,
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = FeatToWaveTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        # text-to-wave
        text_to_feat_checkpoint = os.path.join(text_to_feat_exp_dir, "model", "last.pth")
        feat_to_wave_checkpoint = os.path.join(feat_to_wave_exp_dir, "model", "last.pth")

        # text-to-wave
        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_text_to_wave_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=text_to_wave_exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    vocab_size=VOCAB_SIZE,
                    up_scale=TEXT_TO_FEAT_UP_SCALE * FEAT_TO_WAVE_UP_SCALE,
                    text_to_feat_checkpoint=text_to_feat_checkpoint,
                    feat_to_wave_checkpoint=feat_to_wave_checkpoint,
                ),
                return_hydra_config=True,
            )

        test_dataset = instantiate(config.test.dataset.test)

        test_loader = instantiate(
            config.test.dataloader.test,
            test_dataset,
            collate_fn=default_collate_fn,
        )

        model = instantiate_cascade_text_to_wave(
            config.model,
            text_to_feat_checkpoint=config.test.checkpoint.text_to_feat,
            feat_to_wave_checkpoint=config.test.checkpoint.feat_to_wave,
        )
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        generator = CascadeTextToWaveGenerator(
            test_loader,
            model,
            config=config,
        )
        generator.run()

        monkeypatch.undo()


@pytest.mark.parametrize(
    "dataloader",
    [
        "audyn.utils.data.SequentialBatchDataLoader",
        "audyn.utils.data.DynamicBatchDataLoader",
    ],
)
def test_trainer_for_dataloader(monkeypatch: MonkeyPatch, dataloader: str) -> None:
    """Test BaseTrainer for dataloaders."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_for_dataloader_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    dataloader=dataloader,
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = BaseTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


@pytest.mark.parametrize("train_dump_format", ["torch", "webdataset"])
@pytest.mark.parametrize("preprocess_dump_format", ["torch", "webdataset"])
def test_trainer_for_dump_format_conversion(
    monkeypatch: MonkeyPatch, train_dump_format: str, preprocess_dump_format: str
) -> None:
    """Test BaseTrainer for conversion of dump_format."""
    MAX_SHARD_SIZE = 10000
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3

    if train_dump_format == "webdataset" and preprocess_dump_format == "torch":
        pytest.skip(
            "Pair of train_dump_format=webdataset and preprocess_dump_format=torch "
            "is not supported."
        )

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        list_dir = "./dump/list"
        feature_dir = "./dump/feature"
        exp_dir = "./exp"

        # set as torch dataset
        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_for_dump_format_conversion(
                    overrides_conf_dir=overrides_conf_dir,
                    feature_dir=feature_dir,
                    list_dir=list_dir,
                    exp_dir=exp_dir,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    dump_format=preprocess_dump_format,
                ),
                return_hydra_config=True,
            )

        os.makedirs(feature_dir, exist_ok=True)
        os.makedirs(list_dir, exist_ok=True)

        train_dataset_cls, _ = _parse_dataset_class(config.train.dataset.train._target_)
        validation_dataset_cls, _ = _parse_dataset_class(config.train.dataset.validation._target_)
        num_features = config.model.in_channels

        if train_dump_format == "torch":
            if train_dataset_cls is WebDatasetWrapper:
                assert validation_dataset_cls is WebDatasetWrapper

                target_dataset = "audyn.utils.data.TorchObjectDataset"
                config = OmegaConf.to_container(config)
                config["train"]["dataset"]["train"].update(
                    {
                        "_target_": target_dataset,
                    }
                )
                config["train"]["dataset"]["validation"].update(
                    {
                        "_target_": target_dataset,
                    }
                )
                config = OmegaConf.create(config)
        elif train_dump_format == "webdataset":
            if train_dataset_cls is TorchObjectDataset:
                assert validation_dataset_cls is TorchObjectDataset

                target_dataset = "audyn.utils.data.WebDatasetWrapper.instantiate_dataset"
                config = OmegaConf.to_container(config)
                config["train"]["dataset"]["train"].update(
                    {
                        "_target_": target_dataset,
                    }
                )
                config["train"]["dataset"]["validation"].update(
                    {
                        "_target_": target_dataset,
                    }
                )
                config = OmegaConf.create(config)
        else:
            raise NotImplementedError(f"{train_dump_format} is not supported.")

        if preprocess_dump_format == "torch":
            for subset in ["train", "validation"]:
                subset_feature_dir = os.path.join(feature_dir, subset)
                paths = []

                os.makedirs(subset_feature_dir, exist_ok=True)

                for idx in range(DATA_SIZE):
                    min_length = config.model.kernel_size + 1 + idx + 1

                    shape = (num_features, min_length + idx + 1)
                    input = torch.full(shape, fill_value=idx, dtype=torch.float)
                    target = torch.tensor(idx, dtype=torch.float)
                    feature = {
                        "input": input,
                        "target": target,
                    }

                    path = os.path.join(subset_feature_dir, f"{idx}.pth")
                    paths.append(path)

                    torch.save(feature, path)

                list_path = os.path.join(list_dir, f"{subset}.txt")

                with open(list_path, mode="w") as f:
                    for path in paths:
                        path = os.path.relpath(path, subset_feature_dir)
                        identifier = path.replace(".pth", "")
                        f.write(f"{identifier}\n")
        elif preprocess_dump_format == "webdataset":
            for subset in ["train", "validation"]:
                subset_feature_dir = os.path.join(feature_dir, subset)
                template_path = os.path.join(subset_feature_dir, "%d.tar")
                identifiers = []

                os.makedirs(subset_feature_dir, exist_ok=True)

                with wds.ShardWriter(template_path, maxsize=MAX_SHARD_SIZE) as sink:
                    for idx in range(DATA_SIZE):
                        min_length = config.model.kernel_size + 1 + idx + 1

                        shape = (num_features, min_length + idx + 1)
                        input = torch.full(shape, fill_value=idx, dtype=torch.float)
                        target = torch.tensor(idx, dtype=torch.float)
                        identifier = f"{idx}"
                        feature = {
                            "__key__": identifier,
                            "input.pth": input,
                            "target.pth": target,
                        }
                        identifiers.append(identifier)

                        sink.write(feature)

                list_path = os.path.join(list_dir, f"{subset}.txt")

                with open(list_path, mode="w") as f:
                    for identifier in identifiers:
                        f.write(f"{identifier}\n")
        else:
            raise NotImplementedError(f"{preprocess_dump_format} is not supported.")

        _validate_dataset_class_by_dump_format(
            config.train.dataset.train, dump_format=train_dump_format
        )
        _validate_dataset_class_by_dump_format(
            config.train.dataset.validation, dump_format=train_dump_format
        )

        # overwrite config.train.dataset and dataloader
        setup_system(config)

        _validate_dataset_class_by_dump_format(
            config.train.dataset.train, dump_format=preprocess_dump_format
        )
        _validate_dataset_class_by_dump_format(
            config.train.dataset.validation, dump_format=preprocess_dump_format
        )

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = BaseTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


def test_trainer_for_list_optimizer(monkeypatch: MonkeyPatch) -> None:
    """Test BaseTrainer for optimizers using ListConfig."""
    DATA_SIZE = 20
    BATCH_SIZE = 2
    ITERATIONS = 3

    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        monkeypatch.chdir(temp_dir)

        overrides_conf_dir = relpath(join(dirname(realpath(__file__)), "_conf_dummy"), os.getcwd())
        exp_dir = "./exp"

        with hydra.initialize(
            version_base="1.2",
            config_path=relpath(config_template_path, dirname(realpath(__file__))),
            job_name="test_driver",
        ):
            config = hydra.compose(
                config_name="config",
                overrides=create_dummy_override(
                    overrides_conf_dir=overrides_conf_dir,
                    exp_dir=exp_dir,
                    data_size=DATA_SIZE,
                    batch_size=BATCH_SIZE,
                    iterations=ITERATIONS,
                    train="dummy_vqvae",
                    model="dummy_vqvae",
                    optimizer="dummy_vqvae",
                    lr_scheduler="dummy_vqvae",
                    criterion="dummy_vqvae",
                ),
                return_hydra_config=True,
            )

        setup_system(config)

        train_dataset = instantiate(config.train.dataset.train)
        validation_dataset = instantiate(config.train.dataset.validation)

        train_loader = instantiate(
            config.train.dataloader.train,
            train_dataset,
            collate_fn=default_collate_fn,
        )
        validation_loader = instantiate(
            config.train.dataloader.validation,
            validation_dataset,
            collate_fn=default_collate_fn,
        )
        loaders = BaseDataLoaders(train_loader, validation_loader)

        model = instantiate_model(config.model)
        model = set_device(
            model,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )
        optimizer = instantiate_optimizer(config.optimizer, model)
        lr_scheduler = instantiate_lr_scheduler(config.lr_scheduler, optimizer)
        criterion = instantiate_criterion(config.criterion)
        criterion = set_device(
            criterion,
            accelerator=config.system.accelerator,
            is_distributed=config.system.distributed.enable,
        )

        trainer = BaseTrainer(
            loaders,
            model,
            optimizer,
            lr_scheduler=lr_scheduler,
            criterion=criterion,
            config=config,
        )
        trainer.run()
        trainer.writer.flush()

        monkeypatch.undo()


def create_dummy_override(
    overrides_conf_dir: str,
    exp_dir: str,
    data_size: int,
    batch_size: int = 1,
    iterations: int = 1,
    system: str = "defaults",
    train: str = "dummy",
    model: str = "dummy",
    criterion: str = "dummy",
    optimizer: str = "dummy",
    lr_scheduler: str = "dummy",
    continue_from: str = "",
    checkpoint: str = "",
) -> List[str]:
    sample_rate = 16000

    override_list = [
        f"system={system}",
        f"train={train}",
        "test=dummy",
        f"model={model}",
        f"optimizer={optimizer}",
        f"lr_scheduler={lr_scheduler}",
        f"criterion={criterion}",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        f"data.audio.sample_rate={sample_rate}",
        f"train.dataset.train.size={data_size}",
        f"train.dataset.validation.size={data_size}",
        f"train.dataloader.train.batch_size={batch_size}",
        f"train.dataloader.validation.batch_size={batch_size}",
        f"train.output.exp_dir={exp_dir}",
        f"train.resume.continue_from={continue_from}",
        f"train.steps.iterations={iterations}",
        f"test.dataset.test.size={data_size}",
        f"test.checkpoint={checkpoint}",
    ]

    if lr_scheduler == "none":
        override_list += ["train.steps.lr_scheduler=''"]

    if system == "cpu_ddp" and IS_WINDOWS:
        port = os.environ["MASTER_PORT"]
        override_list += [f"system.distributed.init_method=tcp://localhost:{port}"]

    return override_list


def create_dummy_gan_override(
    overrides_conf_dir: str,
    exp_dir: str,
    data_size: int,
    batch_size: int = 1,
    iterations: int = 1,
    system: str = "defaults",
    train: str = "dummy_gan",
    model: str = "dummy_gan",
    criterion: str = "dummy_gan",
    use_ema_generator: bool = False,
    use_ema_discriminator: bool = False,
    continue_from: str = "",
) -> List[str]:
    sample_rate = 16000
    ema_target = "audyn.optim.optimizer.ExponentialMovingAverageWrapper.build_from_optim_class"

    if use_ema_generator:
        generator_optimizer_config = [
            f"+optimizer.generator._target_={ema_target}",
            "+optimizer.generator.optimizer_class={_target_:torch.optim.Adam,_partial_:true}",
        ]
    else:
        generator_optimizer_config = [
            "+optimizer.generator._target_=torch.optim.Adam",
        ]

    if use_ema_discriminator:
        generator_discriminator_config = [
            f"+optimizer.discriminator._target_={ema_target}",
            "+optimizer.discriminator.optimizer_class={_target_:torch.optim.Adam,_partial_:true}",
        ]
    else:
        generator_discriminator_config = [
            "+optimizer.discriminator._target_=torch.optim.Adam",
        ]

    overridden_config = [
        f"system={system}",
        f"train={train}",
        "test=dummy",
        f"model={model}",
        "optimizer=gan",
        "lr_scheduler=dummy_gan",
        f"criterion={criterion}",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        f"data.audio.sample_rate={sample_rate}",
        f"train.dataset.train.size={data_size}",
        f"train.dataset.validation.size={data_size}",
        f"train.dataloader.train.batch_size={batch_size}",
        f"train.dataloader.validation.batch_size={batch_size}",
        f"train.output.exp_dir={exp_dir}",
        f"train.resume.continue_from={continue_from}",
        f"train.steps.iterations={iterations}",
    ]
    overridden_config = (
        overridden_config + generator_optimizer_config + generator_discriminator_config
    )

    if system == "cpu_ddp" and IS_WINDOWS:
        port = os.environ["MASTER_PORT"]
        overridden_config += [f"system.distributed.init_method=tcp://localhost:{port}"]

    return overridden_config


def create_dummy_text_to_feat_override(
    overrides_conf_dir: str,
    exp_dir: str,
    data_size: int,
    batch_size: int = 1,
    iterations: int = 1,
    vocab_size: int = 10,
    n_mels: int = 5,
    up_scale: int = 2,
    train: str = "dummy_text-to-feat",
    model: str = "dummy_text-to-feat",
    pretrained_feat_to_wave: Optional[str] = None,
) -> List[str]:
    sample_rate = 16000
    length = 10

    output_dir, *_, tag = exp_dir.rsplit("/", maxsplit=2)
    tensorboard_dir = os.path.join(output_dir, "tensorboard", tag)

    if pretrained_feat_to_wave is not None:
        train = f"{train}+pretrained_feat-to-wave"

    if model == "dummy_glowtts":
        criterion = "dummy_glowtts"
    else:
        criterion = "dummy"

    override_list = [
        f"train={train}",
        "test=dummy",
        f"model={model}",
        "optimizer=dummy",
        "lr_scheduler=dummy",
        f"criterion={criterion}",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        f"data.audio.sample_rate={sample_rate}",
        f"train.dataset.train.size={data_size}",
        f"train.dataset.validation.size={data_size}",
        f"train.dataset.train.vocab_size={vocab_size}",
        f"train.dataset.train.n_mels={n_mels}",
        f"train.dataset.train.up_scale={up_scale}",
        f"train.dataset.train.length={length}",
        f"train.dataloader.train.batch_size={batch_size}",
        f"train.dataloader.validation.batch_size={batch_size}",
        f"train.output.exp_dir={exp_dir}",
        f"train.output.tensorboard_dir={tensorboard_dir}",
        f"train.steps.iterations={iterations}",
    ]

    if model == "dummy_glowtts":
        override_list += [
            "train.dataset.train.channels_last=false",
            f"model.encoder.word_embedding.num_embeddings={vocab_size}",
            f"model.decoder.in_channels={n_mels}",
        ]
    else:
        override_list += [
            f"model.vocab_size={vocab_size}",
            f"model.num_features={n_mels}",
            "criterion.criterion_name.key_mapping.estimated.input=estimated_melspectrogram",
            "criterion.criterion_name.key_mapping.target.target=melspectrogram",
        ]

    if pretrained_feat_to_wave is not None:
        override_list += [f"train.pretrained_feat_to_wave.path={pretrained_feat_to_wave}"]

    return override_list


def create_dummy_feat_to_wave_override(
    overrides_conf_dir: str,
    exp_dir: str,
    data_size: int,
    batch_size: int = 1,
    iterations: int = 1,
    n_mels: int = 5,
    up_scale: int = 2,
) -> List[str]:
    sample_rate = 16000
    length = 10

    output_dir, *_, tag = exp_dir.rsplit("/", maxsplit=2)
    tensorboard_dir = os.path.join(output_dir, "tensorboard", tag)

    return [
        "train=dummy_feat-to-wave",
        "test=dummy",
        "model=dummy_feat-to-wave",
        "optimizer=dummy",
        "lr_scheduler=dummy",
        "criterion=dummy",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        f"data.audio.sample_rate={sample_rate}",
        f"train.dataset.train.size={data_size}",
        f"train.dataset.validation.size={data_size}",
        f"train.dataset.train.n_mels={n_mels}",
        f"train.dataset.train.up_scale={up_scale}",
        f"train.dataset.train.length={length}",
        f"train.dataloader.train.batch_size={batch_size}",
        f"train.dataloader.validation.batch_size={batch_size}",
        f"train.output.exp_dir={exp_dir}",
        f"train.output.tensorboard_dir={tensorboard_dir}",
        f"train.steps.iterations={iterations}",
        f"model.num_features={n_mels}",
        "criterion.criterion_name.key_mapping.estimated.input=estimated_waveform",
        "criterion.criterion_name.key_mapping.target.target=waveform",
    ]


def create_dummy_text_to_wave_override(
    overrides_conf_dir: str,
    exp_dir: str,
    data_size: int,
    batch_size: int = 1,
    vocab_size: int = 10,
    up_scale: int = 2,
    text_to_feat_checkpoint: str = None,
    feat_to_wave_checkpoint: str = None,
) -> List[str]:
    sample_rate = 16000
    length = 10

    return [
        "test=dummy_text-to-wave",
        "model=dummy_text-to-wave",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        f"data.audio.sample_rate={sample_rate}",
        f"test.dataset.test.size={data_size}",
        f"test.dataset.test.vocab_size={vocab_size}",
        f"test.dataset.test.up_scale={up_scale}",
        f"test.dataset.test.length={length}",
        f"test.dataloader.test.batch_size={batch_size}",
        f"test.checkpoint.text_to_feat={text_to_feat_checkpoint}",
        f"test.checkpoint.feat_to_wave={feat_to_wave_checkpoint}",
        f"test.output.exp_dir={exp_dir}",
    ]


def create_dummy_for_dataloader_override(
    overrides_conf_dir: str,
    exp_dir: str,
    data_size: int,
    batch_size: int = 1,
    iterations: int = 1,
    dataloader: str = None,
) -> List[str]:
    sample_rate = 16000
    in_channels, out_channels = 3, 2
    kernel_size = 3

    *_, dataloader_name = dataloader.split(".")

    if dataloader_name == "SequentialBatchDataLoader":
        train = "dummy_sequential_dataloader"
        additional_override = [
            f"train.dataloader.train.batch_size={batch_size}",
            f"train.dataloader.validation.batch_size={batch_size}",
        ]
    elif dataloader_name == "DynamicBatchDataLoader":
        train = "dummy_dynamic_dataloader"
        batch_length = 3 * data_size // 4
        additional_override = [
            "train.dataloader.train.key=input",
            "train.dataloader.validation.key=input",
            f"train.dataloader.train.batch_length={batch_length}",
            f"train.dataloader.validation.batch_length={batch_length}",
        ]
    else:
        raise ValueError(f"Invalid dataloader {dataloader_name} is given.")

    override_list = [
        f"train={train}",
        "test=dummy",
        "model=dummy_cnn",
        "optimizer=dummy",
        "lr_scheduler=dummy",
        "criterion=dummy",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        "preprocess.dump_format=torch",
        f"data.audio.sample_rate={sample_rate}",
        f"train.dataset.train.size={data_size}",
        f"train.dataset.validation.size={data_size}",
        f"train.dataset.train.num_features={in_channels}",
        f"train.dataset.train.min_length={kernel_size + 1}",
        f"train.dataloader.train._target_={dataloader}",
        f"train.dataloader.validation._target_={dataloader}",
        f"train.output.exp_dir={exp_dir}",
        f"train.steps.iterations={iterations}",
        f"model.in_channels={in_channels}",
        f"model.out_channels={out_channels}",
        f"model.kernel_size={kernel_size}",
    ]

    return override_list + additional_override


def create_dummy_for_dump_format_conversion(
    overrides_conf_dir: str,
    list_dir: str,
    feature_dir: str,
    exp_dir: str,
    batch_size: int = 1,
    iterations: int = 1,
    dump_format: str = None,
) -> List[str]:
    sample_rate = 16000
    in_channels, out_channels = 3, 2
    kernel_size = 3

    train_list_path = os.path.join(list_dir, "train.txt")
    validation_list_path = os.path.join(list_dir, "validation.txt")
    train_feature_dir = os.path.join(feature_dir, "train")
    validation_feature_dir = os.path.join(feature_dir, "validation")

    override_list = [
        "train=dump_dump-format_conversion",
        "test=dummy",
        "model=dummy_cnn",
        "optimizer=dummy",
        "lr_scheduler=dummy",
        "criterion=dummy",
        f"hydra.searchpath=[{overrides_conf_dir}]",
        "hydra.job.num=1",
        f"hydra.runtime.output_dir={exp_dir}/log",
        f"data.audio.sample_rate={sample_rate}",
        f"preprocess.dump_format={dump_format}",
        f"train.dataset.train.list_path={train_list_path}",
        f"train.dataset.train.feature_dir={train_feature_dir}",
        f"train.dataset.validation.list_path={validation_list_path}",
        f"train.dataset.validation.feature_dir={validation_feature_dir}",
        f"train.dataloader.train.batch_size={batch_size}",
        "train.dataloader.validation.batch_size=1",
        f"train.output.exp_dir={exp_dir}",
        f"train.steps.iterations={iterations}",
        f"model.in_channels={in_channels}",
        f"model.out_channels={out_channels}",
        f"model.kernel_size={kernel_size}",
    ]

    return override_list


def pad_collate_fn(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    dict_batch = default_collate_fn(batch)
    dict_batch["initial_state"] = torch.zeros((dict_batch["input"].size(0), 1), dtype=torch.float)

    if "target" in dict_batch:
        max_length = dict_batch["target"].size(-1)
    else:
        max_length = dict_batch["input"].size(-1)

    dict_batch["max_length"] = max_length

    return dict_batch


def gan_collate_fn(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    dict_batch = pad_collate_fn(batch)
    dict_batch = make_noise(dict_batch, key_mapping={"input": "noise"})

    return dict_batch


def _parse_dataset_class(target: str) -> Tuple[Any, Any]:
    mod_name, var_name = target.rsplit(".", maxsplit=1)

    try:
        fn_name = None
        imported_module = importlib.import_module(mod_name)
    except ModuleNotFoundError:
        fn_name = var_name
        mod_name, var_name = mod_name.rsplit(".", maxsplit=1)
        imported_module = importlib.import_module(mod_name)

    train_dataset_cls = getattr(imported_module, var_name)

    return train_dataset_cls, fn_name


def _validate_dataset_class_by_dump_format(config: DictConfig, dump_format) -> None:
    train_dataset_cls, fn_name = _parse_dataset_class(config._target_)

    if dump_format == "torch":
        assert train_dataset_cls is TorchObjectDataset
        assert fn_name is None
    elif dump_format == "webdataset":
        assert train_dataset_cls is WebDatasetWrapper
        assert fn_name == "instantiate_dataset"
    else:
        raise NotImplementedError(f"{dump_format} is not supported.")

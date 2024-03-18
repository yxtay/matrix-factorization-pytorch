import contextlib
import datetime
import os
from pathlib import Path

import flaml
import lightning as L
import lightning.pytorch.loggers as pl_loggers
import mlflow
import ray.train
import ray.train.lightning as ray_lightning
import ray.train.torch as ray_torch
import ray.tune
import ray.tune.schedulers as ray_schedulers
import ray.tune.stopper as ray_stopper

from .data.load import Movielens1mPipeDataModule
from .lightning import METRIC, LitMatrixFactorization


def prepare_trainer(config):
    logger = pl_loggers.TensorBoardLogger(
        save_dir=config["tensorboard_save_dir"],
        name=ray.train.get_context().get_experiment_name(),
        log_graph=True,
        default_hp_metric=False,
    )
    trainer = L.Trainer(
        precision=config["precision"],
        max_epochs=config["max_epochs"],
        max_time=config["max_time"],
        enable_checkpointing=False,
        enable_progress_bar=False,
        enable_model_summary=False,
        strategy=ray_lightning.RayDDPStrategy(),
        logger=False if ray.train.get_context().get_world_rank() else logger,
        callbacks=[ray_lightning.RayTrainReportCallback()],
        plugins=[ray_lightning.RayLightningEnvironment()],
    )
    return ray_lightning.prepare_trainer(trainer)


def mlflow_start_run(mlflow_tracking_uri):
    experiment_name = ray.train.get_context().get_experiment_name()
    if ray.train.get_context().get_world_rank() or experiment_name is None:
        return contextlib.nullcontext()

    mlflow.set_tracking_uri(mlflow_tracking_uri)
    mlflow.pytorch.autolog(
        checkpoint_monitor=METRIC["name"], checkpoint_mode=METRIC["mode"]
    )

    if experiment := mlflow.get_experiment_by_name(experiment_name):
        experiment_id = experiment.experiment_id
    else:
        experiment_id = mlflow.create_experiment(experiment_name)
    trial_name = ray.train.get_context().get_trial_name()
    return mlflow.start_run(experiment_id=experiment_id, run_name=trial_name)


def train_loop_per_worker(config):
    trainer = prepare_trainer(config)

    with trainer.init_module():
        num_embeddings = 2 ** config["num_embeddings_exp"] + 1
        max_norm = 2.0 ** config["max_norm_exp"] if config["use_max_norm"] else None

        datamodule = Movielens1mPipeDataModule(
            data_dir=config["data_dir"],
            batch_size=2 ** config["batch_size_exp"],
            num_hashes=config["num_hashes"],
            num_buckets=num_embeddings,
        )
        model = LitMatrixFactorization(
            num_embeddings=num_embeddings,
            embedding_dim=2 ** config["embedding_dim_exp"],
            train_loss=config["train_loss"],
            max_norm=max_norm,
            sparse=config["sparse"],
            normalize=config["normalize"],
            use_user_negatives=config["use_user_negatives"],
            learning_rate=config["learning_rate"],
        )

    ckpt_path = None
    if checkpoint := ray.train.get_checkpoint():
        checkpoint_name = ray_lightning.RayTrainReportCallback.CHECKPOINT_NAME
        with checkpoint.as_directory() as ckpt_dir:
            ckpt_path = Path(ckpt_dir, checkpoint_name)

    with mlflow_start_run(config["mlflow_tracking_uri"]):
        mlflow.log_params(config)
        mlflow.log_params(datamodule.hparams)
        mlflow.log_params(model.hparams)
        trainer.fit(model, datamodule=datamodule, ckpt_path=ckpt_path)


def get_run_config():
    return ray.train.RunConfig(
        storage_path=Path("ray_results").absolute(),
        checkpoint_config=ray.train.CheckpointConfig(
            num_to_keep=1,
            checkpoint_score_attribute=METRIC["name"],
            checkpoint_score_order=METRIC["mode"],
        ),
        stop=ray_stopper.ExperimentPlateauStopper(
            metric=METRIC["name"], mode=METRIC["mode"]
        ),
    )


def get_ray_trainer():
    train_loop_config = {
        # tracking
        "tensorboard_save_dir": Path("lightning_logs").absolute(),
        "mlflow_tracking_uri": Path("mlruns").absolute(),
        # trainer
        "precision": "bf16-true",
        "max_epochs": 1,
        "max_time": datetime.timedelta(hours=1),
        # datamodule
        "data_dir": Path("data").absolute(),
        "batch_size_exp": 10,
        "num_hashes": 2,
        # lightning module
        "num_embeddings_exp": 16,
        "embedding_dim_exp": 5,
        "train_loss": "AlignmentContrastiveLoss",
        "use_max_norm": False,
        "max_norm_exp": 0,
        "sparse": True,
        "normalize": True,
        "use_user_negatives": True,
        "learning_rate": 1.0,
    }
    scaling_config = ray.train.ScalingConfig(
        num_workers=1,
        resources_per_worker={"CPU": os.cpu_count() - 1},
    )
    return ray_torch.TorchTrainer(
        train_loop_per_worker=train_loop_per_worker,
        train_loop_config=train_loop_config,
        scaling_config=scaling_config,
        run_config=get_run_config(),
    )


def get_tuner():
    train_losses = [
        "PairwiseLogisticLoss",
        # "PairwiseHingeLoss",
        "AlignmentContrastiveLoss",
        # "AlignmentUniformityLoss",
        # "MutualInformationNeuralEstimatorLoss",
    ]
    search_space = {
        "num_hashes": ray.tune.randint(1, 5),
        "num_embeddings_exp": ray.tune.randint(10, 17),
        "embedding_dim_exp": ray.tune.randint(2, 7),
        "train_loss": ray.tune.choice(train_losses),
        # "use_max_norm": ray.tune.choice([False, True]),
        # "max_norm_exp": ray.tune.randint(0, 6),
        # "normalize": ray.tune.choice([True, False]),
        "use_user_negatives": ray.tune.choice([True, False]),
        "learning_rate": ray.tune.loguniform(0.1, 10.0),
        "precision": ray.tune.choice(["bf16-true", "bf16-mixed"]),
    }
    low_cost_partial_config = {
        "num_hashes": 1,
        "num_embeddings_exp": 10,
        "embedding_dim_exp": 2,
        # "train_loss": "AlignmentContrastiveLoss",
        # "use_max_norm": False,
        # "max_norm_exp": 0,
        # "normalize": True,
        "use_user_negatives": False,
        "learning_rate": 1.0,
        "precision": "bf16-true",
    }
    point_to_evaluate = {
        "num_hashes": 2,
        "num_embeddings_exp": 16,
        "embedding_dim_exp": 5,
        "train_loss": "AlignmentContrastiveLoss",
        # "use_max_norm": False,
        # "max_norm_exp": 0,
        # "normalize": True,
        "use_user_negatives": True,
        "learning_rate": 1.0,
        "precision": "bf16-true",
    }
    search_alg = flaml.BlendSearch(
        low_cost_partial_config={"train_loop_config": low_cost_partial_config},
        points_to_evaluate=[{"train_loop_config": point_to_evaluate}],
    )
    scheduler = ray_schedulers.AsyncHyperBandScheduler()
    tune_config = ray.tune.TuneConfig(
        metric=METRIC["name"],
        mode=METRIC["mode"],
        search_alg=search_alg,
        scheduler=scheduler,
        num_samples=-1,
        time_budget_s=25000,
        max_concurrent_trials=1,
    )
    tuner = ray.tune.Tuner(
        get_ray_trainer(),
        param_space={"train_loop_config": search_space},
        tune_config=tune_config,
        run_config=get_run_config(),
    )
    return tuner


if __name__ == "__main__":
    import rich

    tuner = get_tuner()
    results = tuner.fit()
    rich.print(results.experiment_path)
    best_results = results.get_best_result()
    rich.print(best_results.path)
    rich.print(best_results.metrics)
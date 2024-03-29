import lightning as L
import lightning.pytorch.callbacks as pl_callbacks
import lightning.pytorch.loggers as pl_loggers
import mlflow
import torch
import torchmetrics
import torchmetrics.retrieval as tm_retrieval

from . import losses as mf_losses
from . import models as mf_models

METRIC = {"name": "val/RetrievalNormalizedDCG", "mode": "max"}


class LitMatrixFactorization(L.LightningModule):
    def __init__(
        self,
        num_embeddings: int = 2**16 + 1,
        embedding_dim: int = 32,
        train_loss: str = "PairwiseHingeLoss",
        *,
        max_norm: float = None,
        sparse: bool = True,
        normalize: bool = True,
        learning_rate: float = 0.1,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()
        self.model = None
        self.example_input_array = self.get_example_input_array()
        self.loss_fns = self.get_loss_fns()
        self.metrics = self.get_metrics(top_k=20)

    def forward(self, batch: dict[str, torch.Tensor]) -> torch.Tensor:
        user_feature_hashes = batch["user_feature_hashes"]
        # shape: (batch_size, num_user_features)
        item_feature_hashes = batch["item_feature_hashes"]
        # shape: (batch_size, num_user_features)
        user_feature_weights = batch["user_feature_weights"]
        # shape: (batch_size, num_item_features)
        item_feature_weights = batch["item_feature_weights"]
        # shape: (batch_size, num_item_features)
        # output shape: (batch_size)
        return self.model(
            user_feature_hashes,
            item_feature_hashes,
            user_feature_weights=user_feature_weights,
            item_feature_weights=item_feature_weights,
        )

    def compute_losses(
        self, batch: dict[str, torch.Tensor], step_name: str = "train"
    ) -> dict[str, torch.Tensor]:
        label = batch["label"]
        # shape: (batch_size)
        sample_weight = batch["weight"]
        # shape: (batch_size)

        # user
        user_idx = batch["user_idx"]
        # shape: (batch_size)
        user_feature_hashes = batch["user_feature_hashes"]
        # shape: (batch_size, num_user_features)
        user_feature_weights = batch.get("user_feature_weights")
        # shape: (batch_size, num_user_features)

        # item
        item_idx = batch["item_idx"]
        # shape: (batch_size)
        item_feature_hashes = batch["item_feature_hashes"]
        # shape: (batch_size, num_item_features)
        item_feature_weights = batch.get("item_feature_weights")
        # shape: (batch_size, num_item_features)

        # negative item
        neg_item_idx = batch.get("neg_item_idx")
        # shape: (batch_size, neg_multiple)
        neg_item_feature_hashes = batch.get("neg_item_feature_hashes")
        # shape: (batch_size, neg_multiple, num_item_features)
        neg_item_feature_weights = batch.get("neg_item_feature_weights")
        # shape: (batch_size, neg_multiple, num_item_features)

        user_embed = self.model.embed(user_feature_hashes, user_feature_weights)
        # shape: (batch_size, embed_dim)
        item_embed = self.model.embed(item_feature_hashes, item_feature_weights)
        # shape: (batch_size, embed_dim)
        if neg_item_feature_hashes is not None:
            num_item_features = neg_item_feature_hashes.size(-1)
            # reshape to 2d
            neg_item_feature_hashes = neg_item_feature_hashes.reshape(
                -1, num_item_features
            )
            # shape: (batch_size * neg_multiple, num_item_features)
            neg_item_feature_weights = neg_item_feature_weights.reshape(
                -1, num_item_features
            )
            # shape: (batch_size * neg_multiple, num_item_features)
            neg_item_embed = self.model.embed(
                neg_item_feature_hashes, neg_item_feature_weights
            )
            # shape: (batch_size * neg_multiple, embed_dim)
            item_embed = torch.cat([item_embed, neg_item_embed])
            # shape: (batch_size * (1 + neg_multiple), embed_dim)
            item_idx = torch.cat([item_idx, neg_item_idx.reshape(-1)])
            # shape: (batch_size * (1 + neg_multiple))

        losses = {}
        for loss_fn in self.loss_fns:
            key = f"{step_name}/{loss_fn.__class__.__name__}"
            losses[key] = loss_fn(
                user_embed=user_embed,
                item_embed=item_embed,
                label=label,
                sample_weight=sample_weight,
                user_idx=user_idx,
                item_idx=item_idx,
            )

        return losses

    def update_metrics(
        self, batch: dict[str, torch.Tensor], step_name: str = "train"
    ) -> torchmetrics.MetricCollection:
        user_idx = batch["user_idx"].long()
        label = batch["label"]
        score = self(batch)
        metrics = self.metrics[step_name]
        metrics.update(preds=score, target=label, indexes=user_idx)
        return metrics

    def on_fit_start(self) -> None:
        if self.global_rank == 0:
            metrics = {key.replace("val/", "hp/"): 0.0 for key in self.metrics["val"]}
            self.logger.log_hyperparams(params=self.hparams, metrics=metrics)
            self.logger.log_graph(self)

    def training_step(
        self, batch: dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        losses = self.compute_losses(batch, step_name="train")
        self.log_dict(losses)
        return losses[f"train/{self.hparams.train_loss}"]

    def validation_step(self, batch: dict[str, torch.Tensor], batch_idx: int) -> None:
        losses = self.compute_losses(batch, step_name="val")
        self.log_dict(losses, sync_dist=True)
        metrics = self.update_metrics(batch, step_name="val")
        self.log_dict(metrics)

    def test_step(self, batch: dict[str, torch.Tensor], batch_idx: int) -> None:
        losses = self.compute_losses(batch, step_name="test")
        self.log_dict(losses, sync_dist=True)
        metrics = self.update_metrics(batch, step_name="test")
        self.log_dict(metrics)

    def predict_step(
        self, batch: dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        return self(batch)

    def on_validation_epoch_end(self) -> None:
        metrics = {
            key.replace("val/", "hp/"): self.trainer.callback_metrics[key]
            for key in self.metrics["val"]
        }
        self.log_dict(metrics, sync_dist=True)

    def configure_optimizers(self) -> torch.optim.Optimizer:
        if self.model.sparse:
            optimizer_class = torch.optim.SparseAdam
        else:
            optimizer_class = torch.optim.AdamW
        return optimizer_class(self.parameters(), lr=self.hparams.learning_rate)

    def configure_callbacks(self) -> list[L.Callback]:
        early_stop = pl_callbacks.EarlyStopping(
            monitor=METRIC["name"], mode=METRIC["mode"]
        )
        checkpoint = pl_callbacks.ModelCheckpoint(
            monitor=METRIC["name"], mode=METRIC["mode"], auto_insert_metric_name=False
        )
        return [early_stop, checkpoint]

    def configure_model(self) -> None:
        if self.model is None:
            self.model = mf_models.MatrixFactorization(
                num_embeddings=self.hparams.num_embeddings,
                embedding_dim=self.hparams.embedding_dim,
                max_norm=self.hparams.max_norm,
                sparse=self.hparams.sparse,
                normalize=self.hparams.normalize,
            )

    def get_loss_fns(self) -> torch.nn.ModuleList:
        loss_fns = [
            loss_cls()
            for loss_cls in [
                mf_losses.AlignmentLoss,
                mf_losses.ContrastiveLoss,
                mf_losses.AlignmentContrastiveLoss,
                mf_losses.UniformityLoss,
                mf_losses.AlignmentUniformityLoss,
                mf_losses.MutualInformationNeuralEstimatorLoss,
                mf_losses.PairwiseHingeLoss,
                mf_losses.PairwiseLogisticLoss,
            ]
        ]
        return torch.nn.ModuleList(loss_fns)

    def get_metrics(self, top_k: int = 20) -> torch.nn.ModuleDict:
        metrics = {
            step_name: torchmetrics.MetricCollection(
                tm_retrieval.RetrievalNormalizedDCG(top_k=top_k),
                tm_retrieval.RetrievalRecall(top_k=top_k),
                tm_retrieval.RetrievalPrecision(top_k=top_k),
                tm_retrieval.RetrievalMAP(top_k=top_k),
                tm_retrieval.RetrievalHitRate(top_k=top_k),
                tm_retrieval.RetrievalMRR(top_k=top_k),
                prefix=f"{step_name}/",
            )
            for step_name in ["val", "test"]
        }
        return torch.nn.ModuleDict(metrics)

    def get_example_input_array(self) -> tuple[dict[str, torch.Tensor]]:
        example_input_array = (
            {
                "user_feature_hashes": torch.zeros(1, 1).int(),
                "item_feature_hashes": torch.zeros(1, 1).int(),
                "user_feature_weights": torch.zeros(1, 1),
                "item_feature_weights": torch.zeros(1, 1),
            },
        )
        return example_input_array


def mlflow_start_run(experiment_name: str = "") -> mlflow.ActiveRun:
    experiment_name = experiment_name or datetime.datetime.now().isoformat()
    experiment_id = mlflow.set_experiment(experiment_name).experiment_id
    return mlflow.start_run(experiment_id=experiment_id)


def get_trainer(experiment_name: str = "") -> L.Trainer:
    experiment_name = experiment_name or datetime.datetime.now().isoformat()
    logger = [
        pl_loggers.TensorBoardLogger(
            save_dir="lightning_logs",
            name=experiment_name,
            log_graph=True,
            default_hp_metric=False,
        ),
        pl_loggers.MLFlowLogger(
            tracking_uri="mlruns",
            experiment_name=experiment_name,
            run_id=mlflow.active_run().info.run_id,
            log_model=True,
        ),
    ]
    callbacks = [pl_callbacks.RichProgressBar()]
    return L.Trainer(
        precision="bf16-mixed",
        logger=logger,
        callbacks=callbacks,
        max_epochs=1,
        max_time=datetime.timedelta(hours=1),
    )


if __name__ == "__main__":
    import datetime

    from .data import load as mf_data

    experiment_name = datetime.datetime.now().isoformat()

    train_losses = [
        "PairwiseLogisticLoss",
        "PairwiseHingeLoss",
        "AlignmentContrastiveLoss",
        "AlignmentUniformityLoss",
        "MutualInformationNeuralEstimatorLoss",
    ]
    for train_loss in train_losses:
        mlflow_start_run(experiment_name)
        trainer = get_trainer(experiment_name)

        with trainer.init_module():
            datamodule = mf_data.Movielens1mPipeDataModule(negative_multiple=1)
            model = LitMatrixFactorization(train_loss=train_loss)

        with mlflow.active_run():
            mlflow.log_params(datamodule.hparams)
            mlflow.log_params(model.hparams)
            trainer.fit(model=model, datamodule=datamodule)

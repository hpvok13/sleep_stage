import os
from typing import Callable, Optional

import hydra
import torch
from matplotlib.pylab import f
from omegaconf import DictConfig, OmegaConf
from sklearn.metrics import accuracy_score
from torch import nn
from torch.nn.utils import clip_grad_norm_, clip_grad_value_
from torch.optim.lr_scheduler import OneCycleLR
from tqdm import tqdm

import wandb
from data import get_dataloaders
from model import Model
from utils import AttrDict


def eval_iter(
    model: nn.Module,
    criterion: nn.Module,
    val_loader: torch.utils.data.DataLoader,
    device: torch.device,
) -> tuple[float, float]:
    """
    Perform a single evaluation iteration.

    Args:
        model (Conv2dEIRNN): The model to be evaluated.
        criterion (torch.nn.Module): The loss function.
        val_loader (torch.utils.data.DataLoader): The test data loader.
        epoch (int): The current epoch number.
        device (torch.device): The device to perform computations on.

    Returns:
        tuple: A tuple containing the test loss and accuracy.
    """
    model.eval()
    test_loss = 0.0
    test_correct = 0
    test_total = 0
    test_correct_weighted = 0
    test_total_weighted = 0

    weight = val_loader.dataset.weight.to(device)

    with torch.no_grad():
        for x, labels in val_loader:
            x = x.to(device)
            labels = labels.to(device)

            # Forward pass
            outputs = model(x)
            loss = criterion(outputs, labels)

            # Update statistics
            test_loss += loss.item()
            predicted = outputs.argmax(-1)
            correct = (predicted == labels).sum().item()
            test_correct += correct
            test_total += len(labels)
            correct_weighted = ((predicted == labels) * weight[labels]).sum().item()
            total_weighted = weight[labels].sum().item()
            test_correct_weighted += correct_weighted
            test_total_weighted += total_weighted

    # Calculate average test loss and accuracy
    test_loss /= len(val_loader)
    test_acc = test_correct / test_total
    test_acc_weighted = test_correct_weighted / test_total_weighted

    return test_loss, test_acc, test_acc_weighted


@hydra.main(
    version_base=None,
    config_path="/om2/user/valmiki/sleep_stage/config",
    config_name="config",
)
def train(config: DictConfig) -> None:
    """
    Train the model using the provided configuration.

    Args:
        config (dict): Configuration parameters.
    """
    # Load the configuration
    config = OmegaConf.to_container(config, resolve=True)
    config = AttrDict(config)

    # Set the matmul precision
    torch.set_float32_matmul_precision(config.train.matmul_precision)
    # Get device and initialize the model
    config.model.input_shape = (
        config.data.batch_size,
        config.model.in_dim,
        int(
            config.data.conjunction.model_epoch_len
            * config.data.conjunction.model_sf
            * config.data.conjunction.context_window
        ),
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = Model(**config.model).to(device)

    # Compile the model if requested
    model = torch.compile(
        model,
        fullgraph=config.compile.fullgraph,
        dynamic=config.compile.dynamic,
        backend=config.compile.backend,
        mode=config.compile.mode,
        disable=config.compile.disable,
    )

    # Initialize the optimizer
    if config.optimizer.fn == "sgd":
        optimizer = torch.optim.SGD(
            model.parameters(),
            lr=config.optimizer.lr,
            momentum=config.optimizer.momentum,
        )
    elif config.optimizer.fn == "adam":
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=config.optimizer.lr,
            betas=(config.optimizer.beta1, config.optimizer.beta2),
        )
    elif config.optimizer.fn == "adamw":
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=config.optimizer.lr,
            betas=(config.optimizer.beta1, config.optimizer.beta2),
        )
    else:
        raise NotImplementedError(f"Optimizer {config.optimizer.fn} not implemented")

    # Get the data loaders
    train_loader, test_loader, val_loader = get_dataloaders(config.data)

    # Initialize the loss function
    criterion = torch.nn.CrossEntropyLoss(train_loader.dataset.weight).to(device)

    # Initialize the learning rate scheduler
    if config.scheduler.fn is None:
        scheduler = None
    if config.scheduler.fn == "one_cycle":
        scheduler = OneCycleLR(
            optimizer,
            max_lr=config.optimizer.lr,
            total_steps=config.train.epochs * len(train_loader),
        )
    else:
        raise NotImplementedError(f"Scheduler {config.scheduler.fn} not implemented")

    # Initialize Weights & Biases
    if config.wandb:
        wandb.init(project="Sleep Stage", config=config)

    # Create the checkpoint directory
    if config.wandb:
        checkpoint_dir = os.path.join(config.checkpoint.root, wandb.run.name)
    else:
        checkpoint_dir = config.checkpoint.root
    os.makedirs(checkpoint_dir, exist_ok=True)

    global_step = 0

    for epoch in range(config.train.epochs):
        if config.wandb:
            wandb.log(dict(epoch=epoch), step=global_step)

        # Train the model
        train_loss, train_acc, train_acc_weighted, global_step = train_iter(
            config,
            model,
            optimizer,
            scheduler,
            criterion,
            train_loader,
            epoch,
            global_step,
            device,
        )

        if config.wandb:
            wandb.log(
                dict(
                    train_loss=train_loss,
                    train_acc=train_acc,
                    train_acc_weighted=train_acc_weighted,
                ),
                step=global_step,
            )

        # Evaluate the model on the validation set
        test_loss, test_acc, test_acc_weighted = eval_iter(
            model, criterion, test_loader, device
        )
        if config.wandb:
            wandb.log(
                dict(
                    test_loss=test_loss,
                    test_acc=test_acc,
                    test_acc_weighted=test_acc_weighted,
                ),
                step=global_step,
            )

        val_loss, val_acc, val_acc_weighted = eval_iter(
            model, criterion, val_loader, device
        )
        if config.wandb:
            wandb.log(
                dict(
                    val_loss=val_loss,
                    val_acc=val_acc,
                    val_acc_weighted=val_acc_weighted,
                ),
                step=global_step,
            )

        # Print the epoch statistics
        print(
            f"Epoch [{epoch}/{config.train.epochs}] | "
            f"Train Loss: {train_loss:.4f} | "
            f"Train Accuracy: {train_acc:.2%} | "
            f"Train Accuracy Weighted: {train_acc_weighted:.2%} |"
            f"Test Loss: {test_loss:.4f} |"
            f"Test Accuracy: {test_acc:.2%} |"
            f"Test Accuracy Weighted: {test_acc_weighted:.2%} |"
            f"Val Loss: {val_loss:.4f} |"
            f"Val Accuracy: {val_acc:.2%} |"
            f"Val Accuracy Weighted: {val_acc_weighted:.2%}"
        )

        # Save the model
        file_path = os.path.abspath(
            os.path.join(checkpoint_dir, f"checkpoint_{epoch}.pt")
        )
        link_path = os.path.abspath(os.path.join(checkpoint_dir, "checkpoint.pt"))
        checkpoint = {
            "epoch": epoch,
            "model_state_dict": getattr(model, "_orig_mod", model).state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
        }
        torch.save(checkpoint, file_path)
        try:
            os.remove(link_path)
        except FileNotFoundError:
            pass
        os.symlink(file_path, link_path)


if __name__ == "__main__":
    train()

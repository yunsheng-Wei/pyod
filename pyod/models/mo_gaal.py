# -*- coding: utf-8 -*-
"""Multiple-Objective Generative Adversarial Active Learning.
Part of the codes are adapted from
https://github.com/leibinghe/GAAL-based-outlier-detection
"""
# Author: Zhuo Xiao <zhuoxiao@usc.edu>

from collections import defaultdict

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from sklearn.utils import check_array
from sklearn.utils.validation import check_is_fitted
from torch.utils.data import DataLoader, TensorDataset

from .base import BaseDetector
from .gaal_base import create_discriminator, create_generator


class PyODDataset(torch.utils.data.Dataset):
    """Custom Dataset for handling data operations in PyTorch for outlier detection."""

    def __init__(self, X):
        super(PyODDataset, self).__init__()
        self.X = torch.tensor(X, dtype=torch.float32)

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.X[idx]


class MO_GAAL(BaseDetector):
    """Multi-Objective Generative Adversarial Active Learning.

    MO_GAAL directly generates informative potential outliers to assist the
    classifier in describing a boundary that can separate outliers from normal
    data effectively. Moreover, to prevent the generator from falling into the
    mode collapsing problem, the network structure of SO-GAAL is expanded from
    a single generator (SO-GAAL) to multiple generators with different
    objectives (MO-GAAL) to generate a reasonable reference distribution for
    the whole dataset.
    Read more in the :cite:`liu2019generative`.

    Parameters
    ----------
    contamination : float in (0., 0.5), optional (default=0.1)
        The amount of contamination of the data set, i.e.
        the proportion of outliers in the data set. Used when fitting to
        define the threshold on the decision function.

    k : int, optional (default=10)
        The number of sub generators.

    stop_epochs : int, optional (default=20)
        The number of epochs of training. The number of total epochs equals to three times of stop_epochs.

    lr_d : float, optional (default=0.01)
        The learn rate of the discriminator.

    lr_g : float, optional (default=0.0001)
        The learn rate of the generator.

    momentum : float, optional (default=0.9)
        The momentum parameter for SGD.

    Attributes
    ----------
    decision_scores_ : numpy array of shape (n_samples,)
        The outlier scores of the training data.
        The higher, the more abnormal. Outliers tend to have higher
        scores. This value is available once the detector is fitted.

    threshold_ : float
        The threshold is based on ``contamination``. It is the
        ``n_samples * contamination`` most abnormal samples in
        ``decision_scores_``. The threshold is calculated for generating
        binary outlier labels.

    labels_ : int, either 0 or 1
        The binary labels of the training data. 0 stands for inliers
        and 1 for outliers/anomalies. It is generated by applying
        ``threshold_`` on ``decision_scores_``.
    """

    def __init__(self, k=10, stop_epochs=20, lr_d=0.01, lr_g=0.0001,
                 momentum=0.9, contamination=0.1):
        super(MO_GAAL, self).__init__(contamination=contamination)
        self.k = k
        self.stop_epochs = stop_epochs
        self.lr_d = lr_d
        self.lr_g = lr_g
        self.momentum = momentum
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")
        self.train_history = defaultdict(list)

    def fit(self, X, y=None):
        """Fit detector. y is ignored in unsupervised methods.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The input samples.

        y : Ignored
            Not used, present for API consistency by convention.

        Returns
        -------
        self : object
            Fitted estimator.
        """

        X = check_array(X)
        self._set_n_classes(y)
        self.train_history = defaultdict(list)
        names = locals()
        epochs = self.stop_epochs * 3
        latent_size = X.shape[1]
        data_size = X.shape[0]
        # Create discriminator
        self.discriminator = create_discriminator(latent_size, data_size).to(
            self.device)
        optimizer_d = optim.SGD(self.discriminator.parameters(), lr=self.lr_d,
                                momentum=self.momentum)
        criterion = nn.BCELoss()

        # Create k generators
        for i in range(self.k):
            generator_name = 'sub_generator' + str(i)
            generator = create_generator(latent_size).to(self.device)
            names[generator_name] = generator

            # Define the optimizer for the generator
            optimizer_name = 'optimizer_g' + str(i)
            optimizer_g = optim.SGD(generator.parameters(), lr=self.lr_g,
                                    momentum=self.momentum)
            names[optimizer_name] = optimizer_g

        dataloader = DataLoader(TensorDataset(
            torch.tensor(X, dtype=torch.float32).to(self.device)),
                                batch_size=min(500, data_size),
                                shuffle=True)

        stop = 0

        # Start iteration
        for epoch in range(epochs):
            print('Epoch {} of {}'.format(epoch + 1, epochs))
            for batch_idx, data_batch in enumerate(dataloader):
                # print(f'\nTesting for epoch {epoch + 1} index {batch_idx + 1}:')

                data_batch = data_batch[0].to(self.device)
                batch_size = data_batch.size(0)

                # Generate noise
                noise = torch.rand(batch_size, latent_size, device=self.device)

                # Generate potential outliers
                block = ((1 + self.k) * self.k) // 2
                for i in range(self.k):
                    if i != (self.k - 1):
                        noise_start = int(
                            (((self.k + (self.k - i + 1)) * i) / 2) * (
                                        batch_size // block))
                        noise_end = int(
                            (((self.k + (self.k - i)) * (i + 1)) / 2) * (
                                        batch_size // block))
                        names['noise' + str(i)] = noise[noise_start:noise_end]
                    else:
                        noise_start = int(
                            (((self.k + (self.k - i + 1)) * i) / 2) * (
                                        batch_size // block))
                        names['noise' + str(i)] = noise[noise_start:batch_size]

                    names['generated_data' + str(i)] = names[
                        'sub_generator' + str(i)](names['noise' + str(i)])

                # Concatenate real data to generated data
                all_data = torch.cat(
                    [data_batch] + [names['generated_data' + str(i)] for i in
                                    range(self.k)], dim=0)
                labels = torch.cat(
                    [torch.ones(batch_size, 1, device=self.device),
                     torch.zeros(
                         sum([d.size(0) for d in
                              [names['generated_data' + str(i)] for i in
                               range(self.k)]]), 1, device=self.device)],
                    dim=0)

                # Ensure outputs and labels are the same size
                assert all_data.size(0) == labels.size(
                    0), "Mismatch between all_data and labels sizes"

                # Train discriminator
                self.discriminator.train()
                self.discriminator.zero_grad()
                outputs = self.discriminator(all_data)
                outputs = outputs.view(-1,
                                       1)  # Ensure outputs shape matches labels shape
                discriminator_loss = criterion(outputs, labels)
                discriminator_loss.backward()
                optimizer_d.step()
                self.train_history['discriminator_loss'].append(
                    discriminator_loss.item())

                # Get the target value of sub-generators
                with torch.no_grad():
                    pred_scores = self.discriminator(
                        torch.tensor(X, dtype=torch.float32,
                                     device=self.device)).cpu().numpy().ravel()

                for i in range(self.k):
                    names['T' + str(i)] = np.percentile(pred_scores,
                                                        i / self.k * 100)
                    names['trick' + str(i)] = torch.tensor(
                        [float(names['T' + str(i)])] * names[
                            'noise' + str(i)].size(0),
                        device=self.device).unsqueeze(1)

                # Train generators
                if stop == 0:
                    for i in range(self.k):
                        names['optimizer_g' + str(i)].zero_grad()
                        fake_data = names['sub_generator' + str(i)](
                            names['noise' + str(i)])
                        fake_outputs = self.discriminator(fake_data)
                        generator_loss = criterion(fake_outputs,
                                                   names['trick' + str(i)])
                        generator_loss.backward()
                        names['optimizer_g' + str(i)].step()
                        names['sub_generator' + str(
                            i) + '_loss'] = generator_loss.item()
                        self.train_history[f'sub_generator{i}_loss'].append(
                            generator_loss.item())
                else:
                    for i in range(self.k):
                        with torch.no_grad():
                            fake_data = names['sub_generator' + str(i)](
                                names['noise' + str(i)])
                            fake_outputs = self.discriminator(fake_data)
                            generator_loss = criterion(fake_outputs,
                                                       names['trick' + str(i)])
                            names['sub_generator' + str(
                                i) + '_loss'] = generator_loss.item()
                            self.train_history[
                                f'sub_generator{i}_loss'].append(
                                generator_loss.item())

                generator_loss = np.mean(
                    [names['sub_generator' + str(i) + '_loss'] for i in
                     range(self.k)])
                self.train_history['generator_loss'].append(generator_loss)

                if epoch + 1 > self.stop_epochs:
                    stop = 1

        # Detection result
        decision_scores = self.discriminator(
            torch.tensor(X, dtype=torch.float32,
                         device=self.device)).cpu().detach().numpy()
        self.decision_scores_ = decision_scores.ravel()
        self._process_decision_scores()

        return self

    def decision_function(self, X):
        """Predict raw anomaly score of X using the fitted detector.

        The anomaly score of an input sample is computed based on different
        detector algorithms. For consistency, outliers are assigned with
        larger anomaly scores.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The training input samples. Sparse matrices are accepted only
            if they are supported by the base estimator.

        Returns
        -------
        anomaly_scores : numpy array of shape (n_samples,)
            The anomaly score of the input samples.
        """
        check_is_fitted(self, ['discriminator'])
        X = check_array(X)
        pred_scores = self.discriminator(
            torch.tensor(X, dtype=torch.float32).to(
                self.device)).cpu().detach().numpy().ravel()
        return pred_scores

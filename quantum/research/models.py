"""The two quantum models and the matched classical controls (prd/05 sections 2.4 and 2.5).

Each quantum model and its matched control are tuned over exactly 9 configurations, so neither gets a bigger budget.
Each model: fit(U, s, y) on the training subsample; score(U, s) for any rows; higher = more urgent.
"""
from __future__ import annotations

import itertools

import numpy as np
from sklearn.kernel_approximation import RBFSampler
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from quantum.core import circuit

LAMBDAS = (0.25, 0.5, 1.0)
CS = (0.1, 1.0, 10.0)
GAMMAS = (0.1, 1.0, 10.0)
RFF_DIM = 2 * circuit.N_QUBITS - 1   # 15, matched to phi


def _lr(C: float):
    return make_pipeline(StandardScaler(), LogisticRegression(C=C, class_weight="balanced", max_iter=5000))


class QK1:
    """Fidelity quantum kernel + SVM. Compared against K1."""
    name, control = "QK-1", "K1"

    def __init__(self, lam: float, C: float):
        self.lam, self.C = lam, C

    def fit(self, U, s, y):
        self.U_train = np.asarray(U)
        self.psi_train = circuit.statevectors(self.U_train, self.lam)
        self.svc = SVC(kernel="precomputed", C=self.C, class_weight="balanced")
        self.svc.fit(circuit.kernel(self.psi_train, self.psi_train), y)
        return self

    def score(self, U, s):
        out = []
        for start in range(0, len(U), 2000):
            psi = circuit.statevectors(np.asarray(U[start:start + 2000]), self.lam)
            out.append(self.svc.decision_function(circuit.kernel(psi, self.psi_train)))
        return np.concatenate(out) if out else np.array([])

    def artifact(self) -> dict:
        sv = self.svc.support_
        return {"model": "QK-1", "version": "qk1-v1", "lam": self.lam, "support_u": self.U_train[sv].tolist(),
                "dual_coef": self.svc.dual_coef_[0].tolist(), "intercept": float(self.svc.intercept_[0])}


class QP1:
    """Projected quantum features + classical score, logistic regression. Compared against K4 (the gate)."""
    name, control = "QP-1", "K4"

    def __init__(self, lam: float, C: float, with_score: bool = True):
        self.lam, self.C, self.with_score = lam, C, with_score

    def _X(self, U, s):
        phi = circuit.projected_features(np.asarray(U), self.lam)
        return np.hstack([np.asarray(s).reshape(-1, 1), phi]) if self.with_score else phi

    def fit(self, U, s, y):
        self.model = _lr(self.C).fit(self._X(U, s), y)
        return self

    def score(self, U, s):
        return self.model.decision_function(self._X(U, s))

    def artifact(self) -> dict:
        scaler, lr = self.model.named_steps["standardscaler"], self.model.named_steps["logisticregression"]
        return {"model": "QP-1", "version": "qp1-v1", "lam": self.lam, "x_mean": scaler.mean_.tolist(),
                "x_scale": scaler.scale_.tolist(), "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])}


class QP1a(QP1):
    name, control = "QP-1a", "K2"

    def __init__(self, lam: float, C: float):
        super().__init__(lam, C, with_score=False)


class K0:
    """Logistic regression on u: the floor, not a matched control (3 values of C)."""
    name, control = "K0", None

    def __init__(self, C: float):
        self.C = C

    def fit(self, U, s, y):
        self.model = _lr(self.C).fit(U, y)
        return self

    def score(self, U, s):
        return self.model.decision_function(U)


class K1:
    """RBF-kernel SVM on u."""
    name, control = "K1", None

    def __init__(self, gamma: float, C: float):
        self.gamma, self.C = gamma, C

    def fit(self, U, s, y):
        self.model = SVC(kernel="rbf", gamma=self.gamma, C=self.C, class_weight="balanced").fit(U, y)
        return self

    def score(self, U, s):
        return self.model.decision_function(U)


class K2:
    """15 random Fourier features of u + logistic regression."""
    name, control = "K2", None
    with_score = False

    def __init__(self, gamma: float, C: float, seed: int = 0):
        self.gamma, self.C, self.seed = gamma, C, seed

    def _X(self, U, s):
        Z = self.rff.transform(U)
        return np.hstack([np.asarray(s).reshape(-1, 1), Z]) if self.with_score else Z

    def fit(self, U, s, y):
        self.rff = RBFSampler(gamma=self.gamma, n_components=RFF_DIM, random_state=self.seed).fit(U)
        self.model = _lr(self.C).fit(self._X(U, s), y)
        return self

    def score(self, U, s):
        return self.model.decision_function(self._X(U, s))


class K4(K2):
    """Classical score + 15 random Fourier features, logistic regression: the matched control for QP-1."""
    name = "K4"
    with_score = True


class K3:
    """The classical score alone: the order analysts get today."""
    name, control = "K3", None

    def fit(self, U, s, y):
        return self

    def score(self, U, s):
        return np.asarray(s, dtype=float)


def grid(model_name: str) -> list:
    """The 9 configurations for each tunable model."""
    if model_name in ("QK-1", "QP-1", "QP-1a"):
        return [{"lam": l, "C": c} for l, c in itertools.product(LAMBDAS, CS)]
    if model_name in ("K1", "K2", "K4"):
        return [{"gamma": g, "C": c} for g, c in itertools.product(GAMMAS, CS)]
    if model_name == "K0":
        return [{"C": c} for c in CS]
    return [{}]


MODELS = {"QK-1": QK1, "QP-1": QP1, "QP-1a": QP1a, "K0": K0, "K1": K1, "K2": K2, "K3": K3, "K4": K4}
TUNED = ("QK-1", "QP-1", "QP-1a", "K0", "K1", "K2", "K4")


def build(name: str, config: dict, seed: int = 0):
    cls = MODELS[name]
    if name in ("K2", "K4"):
        return cls(seed=seed, **config)
    return cls(**config)

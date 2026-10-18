"""The model behind one clean interface, so the backend swaps at one point without a rewrite (prd/03 section 3.1).

Now: scikit-learn's histogram gradient boosting, which is already installed. On the VM, set `RF_MODEL_BACKEND` (or pass
`backend=`) to `lightgbm` or `snapml` and nothing else changes: `score()`, the features and the export all stay the
same. Each model saves a small JSON manifest that names its backend, plus the backend's own file; `load()` reads the
manifest and dispatches. The sklearn file is a joblib blob and is the laptop fallback only. LightGBM and Snap ML save
a portable text model, which is what the big-endian s390x VM uses (prd/03 section 3.3, the snapml/LightGBM note).
"""
from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np

DEFAULT_BACKEND = os.environ.get("RF_MODEL_BACKEND", "sklearn")


class Backend:
    """Train, predict a fraud probability in [0, 1], and save/load portably."""

    name = "backend"

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight=None) -> "Backend":
        raise NotImplementedError

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def save(self, path_no_ext: str) -> None:
        raise NotImplementedError

    def load_file(self, path_no_ext: str) -> "Backend":
        raise NotImplementedError


class SklearnGB(Backend):
    """HistGradientBoostingClassifier. Handles imbalance with sample weights (prd/03 A-04)."""

    name = "sklearn"

    def __init__(self, params: Optional[dict] = None):
        self.params = params or {"max_depth": 6, "learning_rate": 0.1, "max_iter": 200, "l2_regularization": 1.0,
                                 "early_stopping": False, "random_state": 0}
        self.clf = None

    def fit(self, X, y, sample_weight=None):
        from sklearn.ensemble import HistGradientBoostingClassifier

        self.clf = HistGradientBoostingClassifier(**self.params)
        self.clf.fit(X, y, sample_weight=sample_weight)
        return self

    def predict_proba(self, X):
        return self.clf.predict_proba(X)[:, 1]

    def save(self, path_no_ext):
        import joblib

        joblib.dump(self.clf, path_no_ext + ".joblib")

    def load_file(self, path_no_ext):
        import joblib

        self.clf = joblib.load(path_no_ext + ".joblib")
        return self


class LightGBMText(Backend):
    """LightGBM, saved as a portable text model (big-endian safe). The VM path; not exercised on the laptop."""

    name = "lightgbm"

    def __init__(self, params: Optional[dict] = None):
        self.params = params or {"objective": "binary", "num_leaves": 63, "learning_rate": 0.1, "n_estimators": 300,
                                 "class_weight": "balanced", "random_state": 0}
        self.clf = None

    def fit(self, X, y, sample_weight=None):
        import lightgbm as lgb

        self.clf = lgb.LGBMClassifier(**self.params)
        self.clf.fit(X, y, sample_weight=sample_weight)
        return self

    def predict_proba(self, X):
        return self.clf.predict_proba(X)[:, 1]

    def save(self, path_no_ext):
        self.clf.booster_.save_model(path_no_ext + ".txt")

    def load_file(self, path_no_ext):
        import lightgbm as lgb

        self.clf = lgb.Booster(model_file=path_no_ext + ".txt")
        return self

    def predict_proba_booster(self, X):
        return self.clf.predict(X)


class SnapMLGB(LightGBMText):
    """IBM Snap ML's boosting. Same text interface; used on LinuxONE if snapml installs (prd/03 section 3.1)."""

    name = "snapml"

    def fit(self, X, y, sample_weight=None):
        from snapml import BoostingMachineClassifier

        self.clf = BoostingMachineClassifier(**(self.params or {}))
        self.clf.fit(X, y, sample_weight=sample_weight)
        return self


_BACKENDS = {"sklearn": SklearnGB, "lightgbm": LightGBMText, "snapml": SnapMLGB}   # the one swap point


class Model:
    """A trained scorer plus the feature names it reads, saved and loaded as one unit."""

    def __init__(self, backend: Backend, features: list, name: str):
        self.backend, self.features, self.name = backend, features, name

    @classmethod
    def make(cls, name: str, features: list, backend: Optional[str] = None) -> "Model":
        backend = backend or DEFAULT_BACKEND
        if backend not in _BACKENDS:
            raise ValueError(f"RF_MODEL_BACKEND={backend!r}; known: {', '.join(_BACKENDS)}")
        return cls(_BACKENDS[backend](), list(features), name)

    def fit(self, X, y, sample_weight=None) -> "Model":
        self.backend.fit(np.asarray(X, dtype=float), np.asarray(y), sample_weight)
        return self

    def predict(self, X) -> np.ndarray:
        return np.clip(self.backend.predict_proba(np.asarray(X, dtype=float)), 0.0, 1.0)

    def save(self, model_dir: str) -> None:
        os.makedirs(model_dir, exist_ok=True)
        base = os.path.join(model_dir, self.name)
        self.backend.save(base)
        with open(base + ".model.json", "w", encoding="utf-8") as fh:
            json.dump({"name": self.name, "backend": self.backend.name, "features": self.features}, fh, indent=1)

    @classmethod
    def load(cls, model_dir: str, name: str) -> "Model":
        base = os.path.join(model_dir, name)
        with open(base + ".model.json", encoding="utf-8") as fh:
            manifest = json.load(fh)
        backend = _BACKENDS[manifest["backend"]]().load_file(base)
        return cls(backend, manifest["features"], manifest["name"])

"""QFM-1, the quantum feature map (prd/05 Part 2.3), simulated exactly with numpy.

8 qubits on a line, 2 layers. One layer: RY(theta_i) then RZ(theta_i) on every qubit, then on neighbour
pairs (0,1),(2,3),(4,5),(6,7) and then (1,2),(3,4),(5,6): RXX(tau) RYY(tau) RZZ(tau) = exp(-i tau/2 (XX+YY+ZZ)).
theta_i = lam * pi * u_i with u in [0, 1].

Statevectors use Qiskit's little-endian order (index = sum of b_q * 2**q), so they compare directly with
qiskit.quantum_info.Statevector. Labelled backend: "numpy-statevector" (exact classical simulation).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

N_QUBITS = 8
LAYERS = 2
TAU = 0.5
CHUNK = 4096

_I2 = np.eye(2, dtype=complex)
_X = np.array([[0, 1], [1, 0]], dtype=complex)
_Y = np.array([[0, -1j], [1j, 0]], dtype=complex)
_Z = np.array([[1, 0], [0, -1]], dtype=complex)


def pairs(n: int = N_QUBITS) -> list:
    """Neighbour pairs in application order: even pairs first, then odd pairs."""
    return [(i, i + 1) for i in range(0, n - 1, 2)] + [(i, i + 1) for i in range(1, n - 1, 2)]


def pair_unitary(tau: float = TAU) -> np.ndarray:
    """RXX(tau) RYY(tau) RZZ(tau) as a 4x4 matrix (the three commute)."""
    c, s = math.cos(tau / 2), math.sin(tau / 2)
    out = np.eye(4, dtype=complex)
    for p in (_X, _Y, _Z):
        out = out @ (c * np.eye(4) - 1j * s * np.kron(p, p))
    return out


def angles(u: np.ndarray, lam: float) -> np.ndarray:
    return lam * math.pi * np.asarray(u, dtype=float)


def _encode_matrices(theta: np.ndarray) -> np.ndarray:
    """Per sample and qubit: RZ(theta) @ RY(theta), shape (N, n, 2, 2)."""
    c, s = np.cos(theta / 2), np.sin(theta / 2)
    em, ep = np.exp(-0.5j * theta), np.exp(0.5j * theta)
    m = np.empty(theta.shape + (2, 2), dtype=complex)
    m[..., 0, 0], m[..., 0, 1] = em * c, -em * s
    m[..., 1, 0], m[..., 1, 1] = ep * s, ep * c
    return m


def _apply_1q(state: np.ndarray, mats: np.ndarray, q: int, n: int) -> np.ndarray:
    N = state.shape[0]
    s = state.reshape(N, 2 ** (n - q - 1), 2, 2 ** q)
    return np.einsum("nab,nibj->niaj", mats, s).reshape(N, 2 ** n)


def _apply_2q(state: np.ndarray, u4: np.ndarray, qa: int, qb: int, n: int) -> np.ndarray:
    lo, hi = min(qa, qb), max(qa, qb)
    N = state.shape[0]
    s = state.reshape(N, 2 ** (n - hi - 1), 2, 2 ** (hi - lo - 1), 2, 2 ** lo)
    t = u4.reshape(2, 2, 2, 2)  # [out_hi, out_lo, in_hi, in_lo]; the gates used are symmetric in the pair
    return np.einsum("xyuv,niujvk->nixjyk", t, s).reshape(N, 2 ** n)


def statevectors(u: np.ndarray, lam: float, n: int = N_QUBITS, layers: int = LAYERS, tau: float = TAU) -> np.ndarray:
    """Exact states for inputs u of shape (N, n). Returns (N, 2**n) complex."""
    u = np.atleast_2d(np.asarray(u, dtype=float))
    if u.shape[1] != n:
        raise ValueError(f"expected {n} inputs per sample, got {u.shape[1]}")
    out = np.empty((u.shape[0], 2 ** n), dtype=complex)
    u4 = pair_unitary(tau)
    for start in range(0, u.shape[0], CHUNK):
        chunk = u[start:start + CHUNK]
        mats = _encode_matrices(angles(chunk, lam))
        st = np.zeros((chunk.shape[0], 2 ** n), dtype=complex)
        st[:, 0] = 1.0
        for _ in range(layers):
            for q in range(n):
                st = _apply_1q(st, mats[:, q], q, n)
            for qa, qb in pairs(n):
                st = _apply_2q(st, u4, qa, qb, n)
        out[start:start + CHUNK] = st
    return out


def _signs(n: int) -> np.ndarray:
    idx = np.arange(2 ** n)
    return np.stack([1 - 2 * ((idx >> q) & 1) for q in range(n)]).astype(float)  # (n, 2**n)


def projected_from_probs(probs: np.ndarray, n: int = N_QUBITS) -> np.ndarray:
    """phi: <Z_i> for each qubit, then <Z_i Z_{i+1}> for each neighbour, shape (N, 2n-1)."""
    z = _signs(n)
    zs = probs @ z.T
    zz = np.stack([probs @ (z[i] * z[i + 1]) for i in range(n - 1)], axis=1)
    return np.hstack([zs, zz])


def projected_features(u: np.ndarray, lam: float, n: int = N_QUBITS) -> np.ndarray:
    psi = statevectors(u, lam, n)
    return projected_from_probs(np.abs(psi) ** 2, n)


def projected_from_shots(u: np.ndarray, lam: float, shots: int, seed: int, n: int = N_QUBITS) -> np.ndarray:
    """phi estimated from sampled bitstrings (finite-shot simulation)."""
    rng = np.random.default_rng(seed)
    probs = np.abs(statevectors(u, lam, n)) ** 2
    est = np.zeros_like(probs)
    for i in range(probs.shape[0]):
        p = probs[i] / probs[i].sum()
        counts = rng.multinomial(shots, p)
        est[i] = counts / shots
    return projected_from_probs(est, n)


def kernel(psi_a: np.ndarray, psi_b: np.ndarray) -> np.ndarray:
    """Fidelity kernel K = |<psi_a|psi_b>|^2."""
    return np.abs(psi_a.conj() @ psi_b.T) ** 2


def qiskit_circuit(n: int = N_QUBITS, layers: int = LAYERS, tau: float = TAU):
    """The same circuit in Qiskit, with one parameter per qubit (the angle theta_i)."""
    from qiskit import QuantumCircuit
    from qiskit.circuit import ParameterVector

    theta = ParameterVector("theta", n)
    qc = QuantumCircuit(n)
    for _ in range(layers):
        for q in range(n):
            qc.ry(theta[q], q)
            qc.rz(theta[q], q)
        for qa, qb in pairs(n):
            qc.rxx(tau, qa, qb)
            qc.ryy(tau, qa, qb)
            qc.rzz(tau, qa, qb)
    return qc, theta


def qiskit_statevectors(u: np.ndarray, lam: float, n: int = N_QUBITS) -> Optional[np.ndarray]:
    """Exact states via qiskit.quantum_info.Statevector, or None if Qiskit is not installed."""
    try:
        from qiskit.quantum_info import Statevector
    except ImportError:
        return None
    qc, theta = qiskit_circuit(n)
    u = np.atleast_2d(u)
    out = []
    for row in angles(u, lam):
        bound = qc.assign_parameters(dict(zip(theta, row)))
        out.append(np.asarray(Statevector(bound).data))
    return np.array(out)

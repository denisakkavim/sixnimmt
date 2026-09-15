"""The typed boundary around emcee's unannotated sampler state."""

from collections.abc import Callable
from typing import Protocol

import emcee
import numpy as np
from numpy.typing import NDArray

from sixnimmt.arena.bots.uncertainty.history import InferenceError


class ProposalKernel(Protocol):
    def __call__(
        self, coordinates: NDArray[np.float64], rng: np.random.RandomState
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]: ...


class PosteriorSampler:
    """Retain emcee state while checking coordinates before domain code sees them."""

    def __init__(
        self,
        coordinates: NDArray[np.float64],
        target: Callable[[NDArray[np.float64]], NDArray[np.float64]],
        proposal: ProposalKernel,
        rng: np.random.RandomState,
    ) -> None:
        self._shape = coordinates.shape
        self._state: emcee.State | None = None
        self._sampler = emcee.EnsembleSampler(
            len(coordinates), coordinates.shape[1], target, moves=emcee.moves.MHMove(proposal), vectorize=True
        )
        self._sampler.random_state = rng.get_state()

    def advance(self, coordinates: NDArray[np.float64], steps: int) -> NDArray[np.float64]:
        initial = coordinates if self._state is None else self._state
        state: object = self._sampler.run_mcmc(initial, steps, skip_initial_state_check=True, store=False)
        if not isinstance(state, emcee.State):
            msg = "sampler returned an invalid state"
            raise InferenceError(msg)
        values: object = state.coords
        if not isinstance(values, np.ndarray) or values.dtype != np.dtype(np.float64) or values.shape != self._shape:
            msg = "sampler returned coordinates with an invalid dtype or shape"
            raise InferenceError(msg)
        if not np.all(np.isfinite(values)):
            msg = "sampler returned non-finite coordinates"
            raise InferenceError(msg)
        self._state = state
        return np.asarray(values, dtype=np.float64)

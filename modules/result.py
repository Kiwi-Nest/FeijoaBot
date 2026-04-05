from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Ok[T]:
    value: T


@dataclass(slots=True, frozen=True)
class Err[E]:
    error: E


type Result[T, E] = Ok[T] | Err[E]

import re
import time
from functools import wraps
from typing import TypeVar, ParamSpec, Callable

P = ParamSpec("P")
R = TypeVar("R")

def normalizeFio(fio: str) -> str:
    """
    Нормализует строку с ФИО
    """
    if not fio:
        return ""

    fio = fio.lower()

    fio = re.sub(r"\(.*?\)", "", fio)
    fio = re.sub(r"\[.*?\]", "", fio)
    fio = re.sub(r"\*.*?\*", "", fio)

    fio = re.sub(r"\s+", " ", fio).strip()

    return fio

def measure_time(func: Callable[P, R]) -> Callable[P, R]: 
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.time()      
        result = func(*args, **kwargs)
        end = time.time()
        print(f"Время выполнения {func.__name__}: {end - start:.4f} сек")
        return result
    return wrapper
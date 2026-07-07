import re
import time
from functools import wraps

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

def measure_time(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()      
        result = func(*args, **kwargs)
        end = time.time()
        print(f"Время выполнения {func.__name__}: {end - start:.4f} сек")
        return result
    return wrapper
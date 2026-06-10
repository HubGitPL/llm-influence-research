from typing import List, Tuple
import random



def stratify_packages(packages: List[str]):
    n = len(packages)

    return {
        "top": packages[: int(n * 0.1)],
        "mid": packages[int(n * 0.1): int(n * 0.4)],
        "tail": packages[int(n * 0.4):],
    }
    
    
    
def sample_representative_packages(
    packages: List[str],
    seed: int = 42,
    n_total: int = 500,
    ratio: Tuple[float, float, float] = (0.3, 0.4, 0.3)
) -> List[str]:

    random.seed(seed)

    strata = stratify_packages(packages)

    n_top = int(n_total * ratio[0])
    n_mid = int(n_total * ratio[1])
    n_tail = n_total - n_top - n_mid

    sample: List[str] = []

    sample += random.sample(strata["top"], min(n_top, len(strata["top"])))
    sample += random.sample(strata["mid"], min(n_mid, len(strata["mid"])))
    sample += random.sample(strata["tail"], min(n_tail, len(strata["tail"])))

    random.shuffle(sample)
    return sample


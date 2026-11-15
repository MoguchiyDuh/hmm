from hmm.algo.fake import FakeAlgorithm
from hmm.algo.models import Algorithm


def get_algorithm() -> Algorithm:
    return FakeAlgorithm()

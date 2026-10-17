from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CORTEX_M = FIXTURES / "cortex-m"


@pytest.fixture
def v1():
    return CORTEX_M / "v1" / "fw.elf", CORTEX_M / "v1" / "fw.map"


@pytest.fixture
def v2():
    return CORTEX_M / "v2" / "fw.elf", CORTEX_M / "v2" / "fw.map"

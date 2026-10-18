"""ESP-IDF builds, whose linker regions do not describe the chip.

Flash is reached through the cache MMU, so irom/drom are address windows of up
to 32 MiB rather than memories, and on the newer chips they are one window
under two names. What actually fills up is the app partition, and the build
already checks the image against it -- so that is what gets recorded:

    app       the .bin the build wrote, against the smallest app partition
    sram_seg  and the other RAM regions, measured as everywhere else

Recognised by flasher_args.json, which ESP-IDF writes next to every app ELF.
"""

import json
import struct
from pathlib import Path

from .log import get_logger

logger = get_logger(__name__)

APP_REGION = "app"

# Cache windows onto flash or PSRAM, named the same on every chip: their
# length is the size of the address window, not of anything behind it.
WINDOWS = frozenset(
    {"irom_seg", "drom_seg", "iram0_2_seg", "drom0_0_seg", "ext_ram_seg", "extern_ram_seg"}
)

PARTITION_MAGIC = b"\xaa\x50"
PARTITION_ENTRY = 32
APP_TYPE = 0x00


def detect(elf_path: Path) -> dict | None:
    """What ESP-IDF wrote about this build, or None for any other build system."""
    flasher = elf_path.parent / "flasher_args.json"
    if not flasher.is_file():
        return None

    try:
        args = json.loads(flasher.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Unreadable {flasher}: {e}")
        return None

    if "app" not in args:
        return None
    return {"build_dir": elf_path.parent, "flasher": args}


def is_dropped(region: str) -> bool:
    """A window, or a reservation that is full by construction and says nothing."""
    return region in WINDOWS or region.endswith("_reserved_seg")


def app_partition(build: dict) -> dict | None:
    """The smallest app partition, which is what the image has to fit in.

    The smallest rather than the one it is flashed to: an OTA update can land
    in any of them, and ESP-IDF's own size check makes the same choice.
    """
    table = build["flasher"].get("partition-table", {}).get("file")
    if not table:
        return None

    try:
        data = (build["build_dir"] / table).read_bytes()
    except OSError as e:
        logger.warning(f"Cannot read the partition table: {e}")
        return None

    apps = []
    for offset in range(0, len(data) - PARTITION_ENTRY + 1, PARTITION_ENTRY):
        entry = data[offset : offset + PARTITION_ENTRY]
        if entry[:2] != PARTITION_MAGIC:
            break
        kind, _, start, size = struct.unpack_from("<BBII", entry, 2)
        if kind == APP_TYPE:
            label = entry[12:28].split(b"\0")[0].decode("ascii", "replace")
            apps.append({"label": label, "offset": start, "size": size})

    return min(apps, key=lambda p: p["size"]) if apps else None


def adjust(usage: dict, build: dict) -> dict:
    """Replace the windows with the image measured against its partition."""
    dropped = sorted(r for r in usage if is_dropped(r))
    result = {r: v for r, v in usage.items() if not is_dropped(r)}

    image = build["build_dir"] / build["flasher"]["app"]["file"]
    partition = app_partition(build)
    if not image.is_file():
        logger.warning(f"ESP-IDF: {image} not found, flash usage is not recorded")
    elif partition is None:
        logger.warning("ESP-IDF: no app partition found, flash usage is not recorded")
    else:
        used = image.stat().st_size
        total = partition["size"]
        result = {
            APP_REGION: {
                "origin": partition["offset"],
                "used": used,
                "total": total,
                "pcnt": used / total * 100 if total else 0,
            },
            **result,
        }

    if dropped:
        logger.info(
            f"ESP-IDF: {', '.join(dropped)} left out (cache windows and fixed reservations); "
            f"flash is the image against the '{partition['label'] if partition else '?'}' partition"
        )
    return result

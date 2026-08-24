"""Map ELF sections onto the physical memory regions declared in the link.

Everything is derived from the loadable segments, so no region name is ever
hardcoded: a project whose flash is called ROM or FLASH_BANK0 works the same as
one that calls it FLASH.
"""

import argparse
import json
import sys
from pathlib import Path

from tabulate import tabulate

from .elf_map_parse import ElfParser, MapParser
from .log import get_logger, setup_logging

USAGE_BAR_LENGTH = 50

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Analyse firmware memory footprint")
    parser.add_argument("-e", "--elf-path", type=Path, required=True, help="Path to the ELF file")
    parser.add_argument("-m", "--map-path", type=Path, required=True, help="Path to the MAP file")
    parser.add_argument(
        "-o", "--output-path", type=Path, required=True, help="Path to the output JSON file"
    )

    return parser.parse_args()


def merge_intervals(intervals: list) -> list:
    """Collapse overlapping [start, end) ranges so nothing is counted twice."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return merged


def compute_memory_usage(map_regions_info: dict, segments: list) -> dict:
    """Charge every loadable segment to the regions its addresses fall into.

    A segment occupies filesz bytes at its load address and memsz bytes at its
    run address. For flash-resident code the two ranges coincide, so the result
    is a union rather than a sum -- adding them would double count. For .data
    they differ, and the union charges the initialiser to flash and the runtime
    copy to RAM without anyone naming either region.
    """
    claimed = {name: [] for name in map_regions_info}

    def claim(start: int, size: int) -> None:
        if size <= 0:
            return

        end = start + size
        for name, region in map_regions_info.items():
            reg_start = region["origin"]
            reg_end = reg_start + region["length"]

            overlap_start = max(start, reg_start)
            overlap_end = min(end, reg_end)
            if overlap_start < overlap_end:
                claimed[name].append((overlap_start, overlap_end))

    for segment in segments:
        claim(segment["paddr"], segment["filesz"])  # stored in the image
        claim(segment["vaddr"], segment["memsz"])  # occupied at run time

    result = {}
    for region, intervals in claimed.items():
        used = sum(end - start for start, end in merge_intervals(intervals))
        total = map_regions_info[region]["length"]
        result[region] = {
            "origin": map_regions_info[region]["origin"],
            "used": used,
            "total": total,
            "pcnt": (used / total) * 100 if total > 0 else 0,
        }

    return result


def make_usage_bar(pcnt: float, length: int = USAGE_BAR_LENGTH) -> str:
    """Occupancy meter, not a progress bar: rendered once from a single value."""
    filled = min(int(length * pcnt / 100), length)
    return "█" * filled + "░" * (length - filled)


def print_memory_usage(region_usage: dict) -> None:
    table = [
        [
            region,
            f"0x{int(info['origin']):08x}",
            f"{info['total'] / 1024:.2f}",
            f"{info['used'] / 1024:.2f}",
            f"{info['pcnt']:.2f}%",
            make_usage_bar(info["pcnt"]),
        ]
        for region, info in region_usage.items()
    ]
    headers = ["Region", "Origin", "Total (KB)", "Used (KB)", "Usage %", "Fill"]
    colalign = ("left", "left", "left", "left", "center")
    print(tabulate(table, headers=headers, tablefmt="grid", colalign=colalign))


def analyse(elf_path: Path, map_path: Path) -> dict:
    map_info = MapParser(map_path).get_regions_info()
    elf = ElfParser(elf_path)

    usage = compute_memory_usage(map_info, elf.get_segments_info())
    print_memory_usage(usage)

    # Anything the ELF already knows travels with the numbers, so the caller
    # never has to dig it out and pass it along by hand.
    return {"toolchain": elf.get_toolchain(), "regions": usage}


def main():
    setup_logging()
    args = parse_args()

    logger.info(f"Analysing ELF: {args.elf_path}, MAP: {args.map_path}")

    try:
        result = analyse(args.elf_path, args.map_path)
        args.output_path.parent.mkdir(parents=True, exist_ok=True)
        with args.output_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=4)
        logger.info(f"Data written to {args.output_path}")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

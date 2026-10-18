"""Where the bytes are: each region broken down by object file and symbol.

    fwtrack-size                          this build, per fw_tracking.toml
    fwtrack-size -o before.json           ... and keep it to compare with later
    fwtrack-size --diff before.json       what changed since
    fwtrack-size new.elf --diff old.elf   two builds, each map beside its ELF

The map names the object every input section was taken from, so this needs no
debug info; DWARF only fills in what the map cannot say, such as the source of
a function under LTO. Bytes of an input section that no symbol covers -- string
literals, literal pools, padding inside it -- stay with their object as
"(no symbol)", and what no input section covers is "(other)", so every level
adds up to the region total the tracker records.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from . import __version__, esp_idf
from .analyse import measure
from .config import DEFAULT_ELF, DEFAULT_MAP, find_config, load_config
from .elf_map_parse import FILL, ElfParser, MapParser
from .log import get_logger, setup_logging

logger = get_logger(__name__)

FORMAT = 1
NO_SYMBOL = "(no symbol)"
STRINGS = "(strings)"
CONSTANTS = "(constants)"
OTHER = "(other)"
FILL_UNIT = "(fill)"
MERGE_UNIT = "(merged)"
LTO_UNIT = "(lto)"
UNKNOWN_UNIT = "?"

# Sections the linker may fold into another object's copy: string literals
# (.rodata.str1.1, .rodata.<function>.str1.4) and constants (.rodata.cst8).
MERGEABLE = re.compile(r"\.(str\d+\.\d+|cst\d+)$")

# LTO's partitions are temporary files with a new name on every build.
LTO_OBJECT = re.compile(r"ltrans\d*(\.ltrans)?\.o(bj)?$")
ARCHIVE_MEMBER = re.compile(r"^(.*?)\(([^()]+)\)$")
# Clones and local statics are numbered in the order the compiler met them, so
# the same function or variable can come back with another number next build.
NUMBERED = re.compile(r"(\.(constprop|isra|part|lto_priv|cold|clone|localalias))?(\.\d+)+$")


def unit_name(obj: str | None) -> str:
    """A name for an input object that is the same on any machine."""
    if obj is None:
        return FILL_UNIT
    if obj == "linker stubs":
        return "(linker stubs)"

    member = ARCHIVE_MEMBER.match(obj)
    if member:
        return f"{os.path.basename(member.group(1))}({member.group(2)})"
    if LTO_OBJECT.search(obj):
        return LTO_UNIT
    return _relative(obj)


def _relative(path: str) -> str:
    """Relative to the current directory when inside it, its basename otherwise.

    Absolute paths differ between a laptop and a runner; inside the project the
    relative path is the same on both, and outside it -- toolchain startup
    files, mostly -- the name is all that is worth comparing.
    """
    if not os.path.isabs(path):
        return os.path.normpath(path)
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return os.path.basename(path)
    return rel if not rel.startswith("..") else os.path.basename(path)


def symbol_name(name: str) -> str:
    return NUMBERED.sub(lambda m: m.group(1) or "", name)


def snapshot(elf_path: Path, map_path: Path) -> dict:
    """Region -> unit -> symbol -> bytes, for one build."""
    parsed_map = MapParser(map_path)
    regions = parsed_map.get_regions_info()
    if not regions:
        raise ValueError(f"{map_path} has no memory configuration; is it a GNU ld map?")

    elf = ElfParser(elf_path)
    usage = measure(elf, regions, elf_path)
    esp = esp_idf.detect(elf_path)

    def region_of(addr: int) -> str | None:
        for name, r in regions.items():
            if r["origin"] <= addr < r["origin"] + r["length"]:
                return None if esp and esp_idf.is_dropped(name) else name
        return None

    sections = elf.get_allocated_sections()
    costs = {}
    for s in sections:
        charged = {region_of(s["vma"])}
        if s["stored"] and s["lma"] != s["vma"]:
            charged.add(region_of(s["lma"]))
        if esp and s["stored"] and esp_idf.APP_REGION in usage:
            charged.add(esp_idf.APP_REGION)
        costs[s["name"]] = sorted(r for r in charged if r in usage)

    by_output = defaultdict(list)
    for i in parsed_map.get_input_sections():
        if i["output"] in costs:
            by_output[i["output"]].append(i)

    # Empty sections stay out of the search: one sharing an address with the
    # section a symbol is in would hide it.
    inputs = defaultdict(list)
    merged = defaultdict(list)
    ends = {s["name"]: s["vma"] + s["size"] for s in sections}
    for output, chunks in by_output.items():
        for k, chunk in enumerate(chunks):
            if MERGEABLE.search(chunk["name"]):
                merged[output].append((chunk, _extent(chunks, k, ends[output])))
            elif chunk["size"]:
                inputs[output].append(chunk)
    for chunks in inputs.values():
        chunks.sort(key=lambda i: i["addr"])

    names = {s["index"]: s["name"] for s in sections}
    symbols = _exclusive(_dedupe(s for s in elf.get_symbols() if s["section"] in names))
    names_map = _demangle([s["name"] for s in symbols])
    lookup = None

    units = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    covered = defaultdict(int)

    def charge(section: str, unit: str, symbol: str, size: int) -> None:
        for region in costs[section]:
            units[region][unit][symbol] += size

    for sym in symbols:
        section = names[sym["section"]]
        chunk = _containing(inputs.get(section, ()), sym["addr"])
        unit = unit_name(chunk["object"]) if chunk else UNKNOWN_UNIT

        if unit in (LTO_UNIT, UNKNOWN_UNIT):
            # Only now, since most builds never need it.
            if lookup is None:
                lookup = elf.get_source_lookup() or False
            source = None
            if lookup:
                source = lookup.code(sym["addr"]) if sym["code"] else lookup.data.get(sym["addr"])
            if source:
                unit = _relative(source)
            elif unit == UNKNOWN_UNIT and sym["file"]:
                unit = sym["file"]

        charge(section, unit, symbol_name(names_map[sym["name"]]), sym["size"])
        if chunk:
            covered[id(chunk)] += sym["size"]

    for section, chunks in inputs.items():
        for chunk in chunks:
            rest = chunk["size"] - covered[id(chunk)]
            if rest <= 0:
                continue
            if chunk["name"] == FILL:
                charge(section, FILL_UNIT, "", rest)
            else:
                charge(section, unit_name(chunk["object"]), NO_SYMBOL, rest)

    # ld folds identical strings and constants of every object into the first
    # such section, which would charge one object with every literal in the
    # image. Each object keeps what it brought; what folding saved is a line
    # of its own, so the total stays exact.
    for section, pool in merged.items():
        brought = 0
        for chunk, _ in pool:
            raw = chunk.get("raw", chunk["size"])
            if raw:
                kind = CONSTANTS if ".cst" in chunk["name"] else STRINGS
                charge(section, unit_name(chunk["object"]), kind, raw)
                brought += raw
        saved = sum(extent for _, extent in pool) - brought
        if saved:
            charge(section, MERGE_UNIT, "", saved)

    # Without a map entry a section's unnamed bytes have no object to go to.
    for s in sections:
        if s["name"] in by_output:
            continue
        named = sum(sym["size"] for sym in symbols if names[sym["section"]] == s["name"])
        if s["size"] > named:
            charge(s["name"], UNKNOWN_UNIT, NO_SYMBOL, s["size"] - named)

    result = {}
    for region, info in usage.items():
        attributed = {u: dict(syms) for u, syms in units.get(region, {}).items()}
        rest = info["used"] - sum(sum(s.values()) for s in attributed.values())
        if rest:
            attributed[OTHER] = {"": rest}
        result[region] = attributed

    return {
        "format": FORMAT,
        "tool": f"fw-footprint-tracker {__version__}",
        "elf": str(elf_path),
        "toolchain": elf.get_toolchain(),
        "regions": {r: {"used": i["used"], "total": i.get("total")} for r, i in usage.items()},
        "units": result,
    }


def _extent(chunks: list, k: int, end: int) -> int:
    """Bytes a mergeable section really takes, from where the next one starts.

    Newer ld lists a section folded into another as empty and gives its
    original size on the next line; ld 2.43 and older list it at full size,
    overlapping what follows. Layout order is the same in both.
    """
    following = chunks[k + 1]["addr"] if k + 1 < len(chunks) else end
    return max(0, following - chunks[k]["addr"])


def _containing(chunks, addr: int) -> dict | None:
    lo, hi = 0, len(chunks)
    while lo < hi:
        mid = (lo + hi) // 2
        if chunks[mid]["addr"] <= addr:
            lo = mid + 1
        else:
            hi = mid
    if lo and addr < chunks[lo - 1]["addr"] + chunks[lo - 1]["size"]:
        return chunks[lo - 1]
    return None


def _dedupe(symbols) -> list:
    """One symbol per address: aliases would otherwise count the same bytes twice."""
    best = {}
    for sym in symbols:
        key = (sym["section"], sym["addr"])
        kept = best.get(key)
        if kept is None or (sym["global"] and not kept["global"]):
            best[key] = sym
    return list(best.values())


def _exclusive(symbols: list) -> list:
    """Sizes cut down to the bytes each symbol has to itself.

    Hand-written routines declare an entry stub whose size runs on into the
    function after it -- libgcc's __aeabi_dsub covers all of __adddf3 -- and
    adding both up counts those bytes twice. Each byte goes to the symbol that
    starts closest before it.
    """
    by_section = defaultdict(list)
    for sym in symbols:
        by_section[sym["section"]].append(sym)

    result = []
    for group in by_section.values():
        group.sort(key=lambda s: s["addr"])
        for i, sym in enumerate(group):
            start, end = sym["addr"], sym["addr"] + sym["size"]
            taken, cursor = 0, start
            for later in group[i + 1 :]:
                if later["addr"] >= end:
                    break
                lo, hi = max(later["addr"], cursor), min(later["addr"] + later["size"], end)
                if hi > lo:
                    taken += hi - lo
                    cursor = hi
            if sym["size"] > taken:
                result.append({**sym, "size": sym["size"] - taken})
    return result


def _demangle(names: list) -> dict:
    """C++ names as written, when a demangler is at hand; untouched otherwise."""
    mapping = {n: n for n in names}
    mangled = [n for n in names if n.startswith("_Z")]
    tool = shutil.which("c++filt")
    if not mangled or not tool:
        return mapping

    try:
        out = subprocess.run(
            [tool], input="\n".join(mangled), capture_output=True, text=True, timeout=30
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return mapping
    if len(out) == len(mangled):
        mapping.update(zip(mangled, out, strict=True))
    return mapping


def load(path: Path, map_path: Path | None = None) -> dict:
    """A snapshot written earlier, or one taken now from an ELF and its map."""
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT:
            raise ValueError(f"{path} is not a snapshot written by fwtrack-size")
        return data

    map_path = map_path or path.with_suffix(".map")
    for p in (path, map_path):
        if not p.is_file():
            raise FileNotFoundError(f"Not found: {p}")
    return snapshot(path, map_path)


def totals(snap: dict) -> dict:
    """Unit -> bytes, per region."""
    return {
        region: {unit: sum(syms.values()) for unit, syms in units.items()}
        for region, units in snap["units"].items()
    }


def diff(base: dict, new: dict) -> list:
    """Per region: before, after, and every unit and symbol that moved."""
    if base.get("tool") != new.get("tool"):
        # Region totals still compare; how bytes are split between objects may
        # have changed between the two, and would show up as movement.
        logger.warning(f"Compared across versions: {base.get('tool')} and {new.get('tool')}")
    order = list(new["regions"]) + [r for r in base["regions"] if r not in new["regions"]]
    report = []
    for region in order:
        b_units = base["units"].get(region, {})
        n_units = new["units"].get(region, {})
        changes = []
        for unit in set(b_units) | set(n_units):
            b_syms, n_syms = b_units.get(unit, {}), n_units.get(unit, {})
            symbols = []
            for name in set(b_syms) | set(n_syms):
                before, after = b_syms.get(name, 0), n_syms.get(name, 0)
                if before != after:
                    symbols.append((name, before, after))
            if symbols:
                before, after = sum(b_syms.values()), sum(n_syms.values())
                symbols.sort(key=lambda s: (-abs(s[2] - s[1]), s[0]))
                changes.append((unit, before, after, symbols))
        changes.sort(key=lambda c: (-abs(c[2] - c[1]), c[0]))

        report.append(
            {
                "region": region,
                "before": base["regions"].get(region, {}).get("used", 0),
                "after": new["regions"].get(region, {}).get("used", 0),
                "total": new["regions"].get(region, base["regions"].get(region, {})).get("total"),
                "changes": changes,
            }
        )
    return report


def _render(rows: list, align: str, headers: list | None = None) -> str:
    """Columns padded by hand: tabulate strips the indent that shows nesting."""
    table = ([headers] if headers else []) + rows
    widths = [max(len(str(row[i])) for row in table) for i in range(len(align))]

    def line(row):
        cells = (
            str(c).rjust(w) if a == "r" else str(c).ljust(w)
            for c, w, a in zip(row, widths, align, strict=True)
        )
        return "  ".join(cells).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)] if headers else []
    return "\n".join(out + [line(row) for row in rows])


def _signed(n: int) -> str:
    return f"{n:+,}" if n else "0"


def _kib(n: int) -> str:
    return f"{n:,} B" if abs(n) < 1024 else f"{n:,} B ({n / 1024:,.1f} KiB)"


def _pct(used: int, total: int | None) -> str:
    return f"{100 * used / total:.1f}%" if total else ""


def _status(before: int, after: int) -> str:
    if not before:
        return "new"
    if not after:
        return "removed"
    return ""


def print_breakdown(snap: dict, top: int, symbols: int) -> None:
    for region, units in snap["units"].items():
        info = snap["regions"][region]
        used, total = info["used"], info.get("total")
        of = f" of {_kib(total)} ({_pct(used, total)})" if total else ""
        print(f"\n{region}: {_kib(used)}{of}")
        if not units:
            continue

        ranked = sorted(units.items(), key=lambda u: -sum(u[1].values()))
        rows = []
        for unit, syms in ranked[: top or None]:
            size = sum(syms.values())
            rows.append([unit, f"{size:,}", _pct(size, used)])
            for name, n in sorted(syms.items(), key=lambda s: -s[1])[: symbols or None]:
                if name:
                    rows.append([f"    {name}", f"{n:,}", ""])
        if len(ranked) > len(ranked[: top or None]):
            rest = sum(sum(s.values()) for _, s in ranked[top:])
            rows.append([f"... {len(ranked) - top} more", f"{rest:,}", _pct(rest, used)])
        print(_render(rows, "lrr", ["Object / symbol", "Bytes", "Share"]))


def print_summary(report: list) -> None:
    rows = [
        [
            r["region"],
            f"{r['before']:,}",
            f"{r['after']:,}",
            _signed(r["after"] - r["before"]),
            _pct(r["after"], r["total"]),
        ]
        for r in report
    ]
    print(_render(rows, "lrrrr", ["Region", "Before", "After", "Δ", "Used"]))


def print_diff(report: list, top: int, symbols: int) -> None:
    print_summary(report)
    for r in report:
        if not r["changes"]:
            continue
        print(f"\n{r['region']}: {_signed(r['after'] - r['before'])} B")
        rows = []
        for unit, before, after, syms in r["changes"][: top or None]:
            rows.append(
                [unit, _signed(after - before), f"{before:,} → {after:,}", _status(before, after)]
            )
            for name, b, a in syms[: symbols or None]:
                if name:
                    rows.append([f"    {name}", _signed(a - b), f"{b:,} → {a:,}", _status(b, a)])
        hidden = r["changes"][top:] if top else []
        if hidden:
            moved = sum(c[2] - c[1] for c in hidden)
            rows.append([f"... {len(hidden)} more", _signed(moved), "", ""])
        print(_render(rows, "lrrl"))


def _path(unit: str) -> list:
    """Where a unit sits in the tree: its directories, then itself."""
    member = ARCHIVE_MEMBER.match(unit)
    # (fill), (lto) and the like are names of their own, not archive members.
    if member and member.group(1):
        return [*member.group(1).split("/"), member.group(2)]
    return unit.split("/")


def _tree(units: dict) -> dict:
    """Units nested by directory; each node holds the sum of what is under it.

    units maps a unit to (values, symbols), values being one number per column.
    """

    def node(width):
        return {"children": {}, "values": [0] * width, "symbols": []}

    root = node(0)
    for unit, (values, symbols) in units.items():
        here = root
        for part in _path(unit):
            here = here["children"].setdefault(part, node(len(values)))
            here["values"] = [a + b for a, b in zip(here["values"], values, strict=True)]
        here["symbols"] = symbols
    return root


def _tree_rows(node: dict, rank, cells, symbols: int | None, depth: int, level: int = 0) -> list:
    rows = []
    for name, child in sorted(node["children"].items(), key=lambda c: (rank(c[1]["values"]), c[0])):
        # A directory holding one thing is shown as one line, the way a path is
        # written: thirdparty/lib/src rather than three levels of the same number.
        while len(child["children"]) == 1 and not child["symbols"]:
            sub, child = next(iter(child["children"].items()))
            name = f"{name}/{sub}"
        rows.append(["  " * level + name, *cells(child["values"])])
        if depth and level + 1 >= depth:
            continue
        rows += _tree_rows(child, rank, cells, symbols, depth, level + 1)
        shown = [] if symbols is None else child["symbols"][: symbols or None]
        for sym, values in shown:
            if sym:
                rows.append(["  " * (level + 1) + "· " + sym, *cells(values)])
    return rows


def print_tree(snap: dict, symbols: int | None, depth: int) -> None:
    for region, units in snap["units"].items():
        info = snap["regions"][region]
        used, total = info["used"], info.get("total")
        of = f" of {_kib(total)} ({_pct(used, total)})" if total else ""
        print(f"\n{region}: {_kib(used)}{of}")
        if not units:
            continue

        tree = _tree(
            {
                unit: (
                    [sum(syms.values())],
                    sorted(([n, [v]] for n, v in syms.items()), key=lambda s: -s[1][0]),
                )
                for unit, syms in units.items()
            }
        )
        rows = _tree_rows(
            tree,
            rank=lambda v: -v[0],
            cells=lambda v, used=used: [f"{v[0]:,}", _pct(v[0], used)],
            symbols=symbols,
            depth=depth,
        )
        print(_render(rows, "lrr", ["Path / symbol", "Bytes", "Share"]))


def print_diff_tree(report: list, symbols: int | None, depth: int) -> None:
    for r in report:
        if not r["changes"]:
            continue
        print(f"\n{r['region']}: {_signed(r['after'] - r['before'])} B")
        tree = _tree(
            {
                unit: ([before, after], [[n, [b, a]] for n, b, a in syms])
                for unit, before, after, syms in r["changes"]
            }
        )
        rows = _tree_rows(
            tree,
            rank=lambda v: -abs(v[1] - v[0]),
            cells=lambda v: [_signed(v[1] - v[0]), f"{v[0]:,} → {v[1]:,}", _status(v[0], v[1])],
            symbols=symbols,
            depth=depth,
        )
        print(_render(rows, "lrrl"))


def markdown_diff(report: list, top: int, symbols: int) -> str:
    """The same report for a merge request comment."""
    lines = ["| Region | Before | After | Δ | Used |", "| --- | ---: | ---: | ---: | ---: |"]
    for r in report:
        delta = r["after"] - r["before"]
        shown = f"**{_signed(delta)}**" if delta else "0"
        used = _pct(r["after"], r["total"])
        lines.append(f"| {r['region']} | {r['before']:,} | {r['after']:,} | {shown} | {used} |")

    for r in report:
        if not r["changes"]:
            continue
        lines += [
            "",
            f"<details><summary>{r['region']}: {_signed(r['after'] - r['before'])} B</summary>",
            "",
            "| Object / symbol | Δ | Before → after |",
            "| --- | ---: | ---: |",
        ]
        for unit, before, after, syms in r["changes"][: top or None]:
            lines.append(f"| `{unit}` | {_signed(after - before)} | {before:,} → {after:,} |")
            for name, b, a in syms[: symbols or None]:
                if name:
                    lines.append(f"| &nbsp;&nbsp;`{name}` | {_signed(a - b)} | {b:,} → {a:,} |")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Break a build down by object and symbol, or compare two builds"
    )
    parser.add_argument(
        "build",
        nargs="?",
        type=Path,
        help="ELF, or a snapshot written by -o (default: the ELF in fw_tracking.toml)",
    )
    parser.add_argument(
        "--diff", type=Path, metavar="BASE", help="ELF or snapshot to compare against"
    )
    parser.add_argument("-m", "--map", type=Path, help="Map of BUILD, if not beside it")
    parser.add_argument("--base-map", type=Path, help="Map of BASE, if not beside it")
    parser.add_argument("-c", "--config", type=Path, help="Tracking config")
    parser.add_argument("-o", "--output", type=Path, help="Save BUILD's snapshot here")
    parser.add_argument("--top", type=int, default=10, help="Objects per region (0: all)")
    parser.add_argument(
        "--symbols",
        type=int,
        help="Symbols per object (0: all; default 5, and none with --tree)",
    )
    parser.add_argument(
        "--tree",
        action="store_true",
        help="Nest objects by directory, each directory with the total of what is in it",
    )
    parser.add_argument(
        "--depth", type=int, default=0, help="Directory levels shown with --tree (0: all)"
    )
    parser.add_argument(
        "--markdown", action="store_true", help="Print the diff as Markdown, for a merge request"
    )
    parser.add_argument(
        "--max-growth",
        type=int,
        metavar="BYTES",
        help="Exit with status 2 if any region grew by more than this",
    )
    return parser.parse_args()


def main():
    setup_logging()
    args = parse_args()
    # A tree is about directories: symbols only when asked for.
    symbols = args.symbols if args.symbols is not None or args.tree else 5

    try:
        build, map_path = args.build, args.map
        if build is None:
            settings = load_config(find_config(args.config)).get("analyse", {})
            build = Path(settings.get("elf", DEFAULT_ELF))
            map_path = map_path or Path(settings.get("map", DEFAULT_MAP))

        new = load(build, map_path)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(new, indent=1) + "\n", encoding="utf-8")
            logger.info(f"Snapshot written to {args.output}")

        if args.diff is None:
            if args.tree:
                print_tree(new, symbols, args.depth)
            else:
                print_breakdown(new, args.top, symbols)
            return

        report = diff(load(args.diff, args.base_map), new)
    except (OSError, ValueError) as e:
        logger.error(e)
        sys.exit(1)

    if args.markdown:
        print(markdown_diff(report, args.top, symbols), end="")
    elif args.tree:
        print_summary(report)
        print_diff_tree(report, symbols, args.depth)
    else:
        print_diff(report, args.top, symbols)

    if args.max_growth is not None:
        grown = [r for r in report if r["after"] - r["before"] > args.max_growth]
        if grown:
            logger.error(
                "Grew by more than "
                f"{args.max_growth:,} B: "
                + ", ".join(f"{r['region']} {r['after'] - r['before']:+,} B" for r in grown)
            )
            sys.exit(2)


if __name__ == "__main__":
    main()

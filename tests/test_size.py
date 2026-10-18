import json
import sys

import pytest

from fwtrack import size


def test_snapshot_adds_up_to_the_regions(v2):
    snap = size.load(v2[0])

    for region, units in snap["units"].items():
        attributed = sum(sum(symbols.values()) for symbols in units.values())
        assert attributed == snap["regions"][region]["used"], region


def test_initialised_data_counts_in_flash_and_ram(v1):
    snap = size.load(v1[0])

    assert snap["units"]["FLASH"]["main.o"]["table"] == 32
    assert snap["units"]["RAM"]["main.o"]["table"] == 32


def test_diff_names_what_changed(v1, v2):
    report = {r["region"]: r for r in size.diff(size.load(v1[0]), size.load(v2[0]))}

    flash = report["FLASH"]
    assert flash["after"] - flash["before"] == sum(c[2] - c[1] for c in flash["changes"])

    units = {unit: (after - before, symbols) for unit, before, after, symbols in flash["changes"]}
    moved = {name: after - before for name, before, after in units["main.o"][1]}
    assert moved["extra_feature"] == 44
    # A string literal has no symbol, and is still found.
    assert moved[size.STRINGS] == len("a string literal has no symbol of its own") + 1
    assert units["libutil.a(util.o)"][0] == 130

    assert report["BACKUP"]["changes"] == []


def test_merged_strings_stay_with_their_objects(v2):
    """main.o and util.o use the same literal; ld keeps one copy for both."""
    flash = size.load(v2[0])["units"]["FLASH"]
    literal = len("a string literal has no symbol of its own") + 1

    assert flash["main.o"][size.STRINGS] == literal
    assert flash["libutil.a(util.o)"][size.STRINGS] == literal
    assert flash[size.MERGE_UNIT][""] == -literal


def test_snapshot_round_trips(tmp_path, v1, v2):
    saved = tmp_path / "before.json"
    saved.write_text(json.dumps(size.load(v1[0])))

    assert size.diff(size.load(saved), size.load(v2[0])) == size.diff(
        size.load(v1[0]), size.load(v2[0])
    )


def test_markdown_is_a_table(v1, v2):
    text = size.markdown_diff(size.diff(size.load(v1[0]), size.load(v2[0])), 10, 5)

    assert "| FLASH | 124 | 328 | **+204** |" in text
    assert "`extra_feature`" in text


@pytest.mark.parametrize(
    "obj, unit",
    [
        ("esp-idf/main/libmain.a(main.c.obj)", "libmain.a(main.c.obj)"),
        ("/opt/toolchain/lib/gcc/arm/libgcc.a(_divdi3.o)", "libgcc.a(_divdi3.o)"),
        ("/var/folders/x/T//ccpJy25d.ltrans0.ltrans.o", size.LTO_UNIT),
        ("CMakeFiles/app.dir/src/main.c.obj", "CMakeFiles/app.dir/src/main.c.obj"),
        ("/opt/toolchain/lib/crtbegin.o", "crtbegin.o"),
        ("linker stubs", "(linker stubs)"),
        (None, size.FILL_UNIT),
    ],
)
def test_units_are_named_the_same_on_any_machine(obj, unit):
    assert size.unit_name(obj) == unit


@pytest.mark.parametrize(
    "name, normal",
    [
        ("foo.constprop.0", "foo.constprop"),
        ("foo.isra.12", "foo.isra"),
        ("bar.lto_priv.0", "bar.lto_priv"),
        ("counter.3", "counter"),
        ("main", "main"),
    ],
)
def test_compiler_numbering_is_dropped(name, normal):
    assert size.symbol_name(name) == normal


def test_overlapping_symbols_are_counted_once():
    # libgcc: an entry stub whose size runs on into the function after it.
    stub = {"name": "__aeabi_dsub", "addr": 0x100, "size": 634, "section": 1}
    body = {"name": "__adddf3", "addr": 0x104, "size": 630, "section": 1}

    sizes = {s["name"]: s["size"] for s in size._exclusive([stub, body])}

    assert sizes == {"__aeabi_dsub": 4, "__adddf3": 630}


def test_max_growth_fails_the_job(monkeypatch, v1, v2, capsys):
    argv = ["fwtrack-size", str(v2[0]), "--diff", str(v1[0]), "--max-growth", "100"]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exit_info:
        size.main()

    assert exit_info.value.code == 2
    assert "+204" in capsys.readouterr().out


def test_tree_nests_by_directory_and_adds_up():
    units = {
        "lib/fatfs/ff.c": ([300], []),
        "lib/fatfs/ffunicode.c": ([700], []),
        "lib/lfs/lfs.c": ([100], []),
        "libc.a(memcpy.o)": ([20], []),
        size.FILL_UNIT: ([4], []),
    }

    rows = size._tree_rows(
        size._tree(units), rank=lambda v: -v[0], cells=lambda v: [v[0]], symbols=None, depth=0
    )

    assert rows == [
        ["lib", 1100],
        ["  fatfs", 1000],
        ["    ffunicode.c", 700],
        ["    ff.c", 300],
        ["  lfs/lfs.c", 100],
        ["libc.a/memcpy.o", 20],
        [size.FILL_UNIT, 4],
    ]

import json
import struct

from fwtrack import esp_idf


def partition(kind: int, subtype: int, offset: int, size: int, label: str) -> bytes:
    return (
        b"\xaa\x50"
        + struct.pack("<BBII", kind, subtype, offset, size)
        + label.encode().ljust(16, b"\0")
        + b"\0" * 4
    )


def esp_build(tmp_path, image_size: int, partitions: list) -> dict:
    (tmp_path / "partition_table").mkdir()
    (tmp_path / "partition_table" / "partition-table.bin").write_bytes(
        b"".join(partitions) + b"\xeb\xeb" + b"\xff" * 30
    )
    (tmp_path / "app.bin").write_bytes(b"\0" * image_size)
    (tmp_path / "flasher_args.json").write_text(
        json.dumps(
            {
                "app": {"offset": "0x10000", "file": "app.bin"},
                "partition-table": {
                    "offset": "0x8000",
                    "file": "partition_table/partition-table.bin",
                },
            }
        )
    )
    return esp_idf.detect(tmp_path / "app.elf")


def test_not_an_esp_build(tmp_path):
    assert esp_idf.detect(tmp_path / "fw.elf") is None


def test_smallest_app_partition_is_the_budget(tmp_path):
    build = esp_build(
        tmp_path,
        1000,
        [
            partition(1, 2, 0x9000, 0x6000, "nvs"),
            partition(0, 0x10, 0x10000, 0x180000, "ota_0"),
            partition(0, 0x11, 0x190000, 0x100000, "ota_1"),
        ],
    )

    assert esp_idf.app_partition(build) == {"label": "ota_1", "offset": 0x190000, "size": 0x100000}


def test_windows_give_way_to_the_image(tmp_path):
    build = esp_build(tmp_path, 256 * 1024, [partition(0, 0, 0x10000, 0x100000, "factory")])
    usage = {
        "irom_seg": {"origin": 0x42000020, "used": 249092, "total": 0xFFFFE0, "pcnt": 1.5},
        "drom_seg": {"origin": 0x42000020, "used": 249092, "total": 0xFFFFE0, "pcnt": 1.5},
        "sram_seg": {"origin": 0x40800000, "used": 66288, "total": 452112, "pcnt": 14.7},
        "lp_reserved_seg": {"origin": 0x50003FE8, "used": 24, "total": 24, "pcnt": 100.0},
    }

    adjusted = esp_idf.adjust(usage, build)

    assert list(adjusted) == ["app", "sram_seg"]
    assert adjusted["app"]["used"] == 256 * 1024
    assert adjusted["app"]["total"] == 0x100000
    assert adjusted["app"]["pcnt"] == 25.0

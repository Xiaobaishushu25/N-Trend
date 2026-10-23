"""Extract archived public fee tables into a dated research-only profile.

HTML is public exchange-standard mirror data; SHFE base rates also checked
against an archived official settlement JSON. Historical schedules are not inferred.
"""
import argparse
import hashlib
import json
import pathlib
import re
from html.parser import HTMLParser


class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        if tag == "tr":
            self.rows.append(self.row)


def rule(text):
    if match := re.fullmatch(r"([\d.]+)元", text):
        return dict(fixed_yuan_per_lot=float(match[1]), notional_rate=0.)
    if match := re.match(r"([\d.]+)/万分之", text):
        return dict(fixed_yuan_per_lot=0., notional_rate=float(match[1]) / 10000)
    raise ValueError("unknown fee notation: " + text)


def build(directory, output):
    directory, output = pathlib.Path(directory), pathlib.Path(output)
    expected = dict(DCE="C EB EG JD L PP V".split(), CZCE="CF CJ FG MA SA SF SH SM SR UR".split(),
                    SHFE="BU PB RB SS".split(), GFEX=["SI"])
    profile = dict(name="published-main-contract-fees-20261022-with-user-SR-override", as_of="2026-10-22",
                   historical_schedule_verified=False, continuous_contract_mapping_verified=False,
                   purpose="offline sensitivity only; never production broker configuration", sources={}, products={})
    for exchange, products in expected.items():
        path = directory / (exchange.lower() + "-mirror.html")
        html = path.read_text(encoding="utf-8")
        parser = Table()
        parser.feed(html)
        url = ("https://quantcont.com" if exchange == "CZCE" else "https://www.quantcont.com") + "/shouxufei/jiaoyisuo/" + exchange
        profile["sources"][exchange] = dict(title=exchange + "交易所标准手续费镜像", url=url,
            retrieved_date="2026-10-22", verification="公开第三方镜像，非交易所官网直接取数；非逐日历史费率",
            snapshot_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        for cells in parser.rows:
            if len(cells) < 11 or not re.fullmatch(r"[A-Za-z]+\d+", cells[1]):
                continue
            product = re.sub(r"\d+$", "", cells[1]).upper()
            if product not in products:
                continue
            if product in profile["products"]:
                raise ValueError("duplicate product row: " + product)
            profile["products"][product] = dict(exchange=exchange, source=exchange, observed_contract=cells[1],
                open=rule(cells[8]), close_yesterday=rule(cells[9]), close_today=rule(cells[10]))
    assert set(profile["products"]) == {p for ps in expected.values() for p in ps}
    official_path = directory / "shfe-settlement-20261021.json"
    official = json.loads(official_path.read_text(encoding="utf-8"))["Settlement"]
    profile["sources"]["SHFE_OFFICIAL"] = dict(title="上期所2026-10-21结算参数",
        url="https://www.shfe.com.cn/data/busiparamdata/future/Settlement20261021.dat",
        verification="直接下载官方结算参数；对应镜像合约开/平昨基准费率一致，平今仍按镜像列",
        snapshot_sha256=hashlib.sha256(official_path.read_bytes()).hexdigest())
    for product in expected["SHFE"]:
        spec = profile["products"][product]
        record = next(r for r in official if r["INSTRUMENTID"].lower() == spec["observed_contract"].lower())
        base = dict(fixed_yuan_per_lot=float(record["TRADEFEEUNIT"]), notional_rate=float(record["TRADEFEERATION"]))
        assert base == spec["open"] == spec["close_yesterday"], (product, base, spec)
        spec["base_crosscheck"] = "SHFE_OFFICIAL"
    profile["products"]["SR"]["published_schedule_before_override"] = {
        k: profile["products"]["SR"].pop(k) for k in ("open", "close_today", "close_yesterday")}
    profile["products"]["SR"].update(roundtrip_fixed_yuan_per_lot=2.,
        user_override="用户明确确认开平往返合计2元/手，所有已平仓糖信号按此口径；不是官方隔夜收费声明")
    with output.open("x", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    print("saved", len(profile["products"]), "products to", output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("output")
    args = parser.parse_args()
    build(args.directory, args.output)

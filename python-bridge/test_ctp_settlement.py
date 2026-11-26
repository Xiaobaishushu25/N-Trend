import unittest

from ctp_settlement import parse_statement


class SettlementStatementTests(unittest.TestCase):
    def test_parse_splits_account_total_and_verified_trade_fees(self):
        content = """
资金状况
手 续 费 Commission： 2.50    行权手续费：0.00
成交记录
|成交日期|交易所|合约|买/卖|成交价|手数|开平|手续费|成交序号|
|20261126|郑商所|MA701|买|2300.00|2|开|2.50|909999990001234567|
|20261126|郑商所|MA701|卖|2311.00|1|平今|0.00|909999990001234568|
平仓明细
|平仓日期|交易所|合约|开仓日期|买/卖|手数|开仓价|成交价|
|20261126|郑商所|MA701|20261126|卖|1|2300.00|2311.00|
"""
        data = parse_statement(content, "20261126")
        self.assertEqual(data["status"], "verified")
        self.assertEqual(data["total_fee_cents"], 250)
        self.assertEqual([row["fee_cents"] for row in data["fills"]], [250, 0])
        self.assertEqual(data["fills"][1]["offset"], "close_today")
        self.assertEqual(data["closes"][0]["open_day"], "20261126")

    def test_incomplete_or_total_only_returns_no_confirmed_trade_rows(self):
        for content in ("资金状况\n手续费 Commission：10.00",
                        "手续费：10.00\n|成交日期|交易所|合约|买/卖|成交价|手数|开平|手续费|成交序号|\n"
                        "|20261126|大商所|PP2701|买|5000|1|开|1.00|000000001|"):
            with self.subTest(content=content):
                result = parse_statement(content, "20261126")
                self.assertEqual(result["status"], "missing_detail")
                self.assertEqual(result["total_fee_cents"], 1000)
                self.assertEqual(result["fills"], [])


if __name__ == "__main__":
    unittest.main()

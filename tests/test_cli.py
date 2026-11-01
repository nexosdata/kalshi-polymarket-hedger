"""Unit tests for command line interface and dashboard renderers."""

import io
import unittest
from unittest.mock import patch
from hedger.cli import (
    build_parser,
    cmd_calc,
    cmd_check,
    cmd_pairs,
    main,
    render_delta_gauge,
)


class TestCLI(unittest.TestCase):
    """Test CLI parser and command handlers."""

    def test_parser_subcommands(self):
        parser = build_parser()

        # monitor subcommand
        args_mon = parser.parse_args(["monitor", "--pair", "FED-MAY-2026-CUT", "--threshold", "20.0"])
        self.assertEqual(args_mon.command, "monitor")
        self.assertEqual(args_mon.pair, "FED-MAY-2026-CUT")
        self.assertEqual(args_mon.threshold, 20.0)

        # check subcommand
        args_chk = parser.parse_args(["check", "--threshold", "15.0"])
        self.assertEqual(args_chk.command, "check")
        self.assertEqual(args_chk.threshold, 15.0)

        # pairs subcommand
        args_prs = parser.parse_args(["pairs"])
        self.assertEqual(args_prs.command, "pairs")

        # calc subcommand
        args_clc = parser.parse_args(["calc", "--contracts", "150", "--price", "0.55"])
        self.assertEqual(args_clc.command, "calc")
        self.assertEqual(args_clc.contracts, 150.0)
        self.assertEqual(args_clc.price, 0.55)

    def test_render_delta_gauge(self):
        gauge_neutral = render_delta_gauge(0.0, threshold=25.0)
        self.assertIn("[NEUTRAL]", gauge_neutral)

        gauge_alert = render_delta_gauge(80.0, threshold=25.0)
        self.assertIn("[DRIFT ALERT]", gauge_alert)

    def test_cmd_pairs_output(self):
        parser = build_parser()
        args = parser.parse_args(["pairs", "--offline"])
        with patch("sys.stdout", new=io.StringIO()) as fake_stdout:
            cmd_pairs(args)
            output = fake_stdout.getvalue()
            self.assertIn("Synchronized Cross-Venue Prediction Pairs", output)
            self.assertIn("FED-MAY-2026-CUT", output)

    def test_cmd_calc_output(self):
        parser = build_parser()
        args = parser.parse_args(["calc", "--contracts", "100", "--price", "0.50"])
        with patch("sys.stdout", new=io.StringIO()) as fake_stdout:
            cmd_calc(args)
            output = fake_stdout.getvalue()
            self.assertIn("CFTC Quadratic Fee (Kalshi)", output)
            self.assertIn("Polygon Gas Cost (Polymarket)", output)
            self.assertIn("$1.7500", output)

    def test_cmd_check_output(self):
        parser = build_parser()
        args = parser.parse_args(["check", "--threshold", "25.0", "--kalshi-contracts", "100.0", "--offline"])
        with patch("sys.stdout", new=io.StringIO()) as fake_stdout:
            cmd_check(args)
            output = fake_stdout.getvalue()
            self.assertIn("PORTFOLIO DELTA NEUTRALITY", output)
            self.assertIn("VENUE EXPOSURE BREAKDOWN", output)

    def test_main_default_execution(self):
        with patch("sys.stdout", new=io.StringIO()) as fake_stdout:
            main(["--offline", "pairs"])
            output = fake_stdout.getvalue()
            self.assertIn("FED-MAY-2026-CUT", output)


if __name__ == "__main__":
    unittest.main()

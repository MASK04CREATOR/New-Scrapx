from app.extract import rule_date
def test_agree(): assert rule_date(["Factsheet as at 31 March 2024 ... NAV as of March 31, 2024"]) == "2024-03-31"
def test_conflict(): assert rule_date(["as at 31 March 2024 and as at 30 April 2024"]) is None
def test_single_hit_goes_to_ai(): assert rule_date(["as at 31 March 2024"]) is None

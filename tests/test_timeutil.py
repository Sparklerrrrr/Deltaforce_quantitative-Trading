from datetime import datetime

from dfq import timeutil as T


def labels(*dts):
    return [f"{d.day:02d} {d:%H:%M}分" for d in dts]


def test_minute_labels_same_day():
    ref = datetime(2026, 11, 13, 21, 30)
    out = T.parse_minute_labels(["13 21:20分", "13 21:25分", "13 21:30分"], ref)
    assert out == [datetime(2026, 11, 13, 21, 20), datetime(2026, 11, 13, 21, 25), datetime(2026, 11, 13, 21, 30)]


def test_minute_labels_cross_day():
    ref = datetime(2026, 11, 13, 0, 10)
    out = T.parse_minute_labels(["12 23:50分", "12 23:55分", "13 00:00分", "13 00:05分"], ref)
    assert out[0] == datetime(2026, 11, 12, 23, 50)
    assert out[-1] == datetime(2026, 11, 13, 0, 5)


def test_minute_labels_cross_month():
    ref = datetime(2026, 12, 1, 0, 30)
    out = T.parse_minute_labels(["30 00:35分", "30 23:55分", "01 00:00分", "01 00:25分"], ref)
    assert out == [datetime(2026, 11, 30, 0, 35), datetime(2026, 11, 30, 23, 55),
                   datetime(2026, 12, 1, 0, 0), datetime(2026, 12, 1, 0, 25)]


def test_minute_labels_cross_year():
    ref = datetime(2027, 1, 1, 0, 5)
    out = T.parse_minute_labels(["31 23:58分", "01 00:02分"], ref)
    assert out == [datetime(2026, 12, 31, 23, 58), datetime(2027, 1, 1, 0, 2)]


def test_minute_labels_leap_day_and_short_february():
    # 2028 是闰年：3 月 1 日的前一天是 2 月 29 日
    out = T.parse_minute_labels(["29 23:59分", "01 00:01分"], datetime(2028, 3, 1, 0, 5))
    assert out[0] == datetime(2028, 2, 29, 23, 59)
    # 2027 不是闰年：前一天是 2 月 28 日
    out = T.parse_minute_labels(["28 23:59分", "01 00:01分"], datetime(2027, 3, 1, 0, 5))
    assert out[0] == datetime(2027, 2, 28, 23, 59)


def test_minute_labels_full_24h_window_is_monotonic():
    ref = datetime(2026, 11, 1, 16, 2)
    pts = [datetime(2026, 10, 31, 16, 5) + (datetime(2026, 11, 1, 16, 0) - datetime(2026, 10, 31, 16, 5)) * i / 287
           for i in range(288)]
    pts = [p.replace(second=0, microsecond=0) for p in pts]
    out = T.parse_minute_labels(labels(*pts), ref)
    assert out == pts
    assert all(a < b for a, b in zip(out, out[1:]))


def test_minute_labels_cached_response_uses_fetch_time_as_ref():
    # 缓存的响应隔天再解析：ref 必须是当初的拉取时刻，而不是现在
    fetched = datetime(2026, 11, 12, 21, 30)
    out = T.parse_minute_labels(["11 21:35分", "12 21:30分"], fetched)
    assert out[-1] == datetime(2026, 11, 12, 21, 30)


def test_minute_labels_bad_input_does_not_crash():
    out = T.parse_minute_labels(["??", "13 21:30分", "", "45 99:99分"], datetime(2026, 11, 13, 21, 30))
    assert out == [None, datetime(2026, 11, 13, 21, 30), None, None]


def test_minute_labels_real_api_format_with_month_and_weekday():
    """2026-11-16 真实接口返回的写法是 "11-15 周日 23:51分"。以前只认文档里的 "25 16:02分"，
    结果每晚 8 个品种、每个 143 个点全部被丢弃（计划里一直提示“分钟曲线为空”）。"""
    ref = datetime(2026, 11, 16, 23, 46)
    out = T.parse_minute_labels(["11-15 周日 23:51分", "11-16 周一 00:01分", "11-16 星期一 23:41分"], ref)
    assert out == [datetime(2026, 11, 15, 23, 51), datetime(2026, 11, 16, 0, 1), datetime(2026, 11, 16, 23, 41)]


def test_minute_labels_real_format_cross_year_and_out_of_range_values():
    out = T.parse_minute_labels(["12-31 周四 23:55分", "01-01 周五 00:05分", "13-40 周五 00:05分", "01-01 周五 25:61分"],
                                datetime(2027, 1, 1, 0, 10))
    assert out == [datetime(2026, 12, 31, 23, 55), datetime(2027, 1, 1, 0, 5), None, None]


def test_minute_labels_real_format_without_weekday_and_mixed_with_doc_format():
    # 不带星期也认；和文档写法混在一起时，文档写法的点以后面那个带月份的点为锚往前推
    ref = datetime(2026, 12, 1, 0, 30)
    out = T.parse_minute_labels(["30 23:50分", "11-30 23:55分", "12-01 周二 00:00分", "01 00:25分"], ref)
    assert out == [datetime(2026, 11, 30, 23, 50), datetime(2026, 11, 30, 23, 55),
                   datetime(2026, 12, 1, 0, 0), datetime(2026, 12, 1, 0, 25)]


def test_minute_labels_real_format_full_day_keeps_every_point():
    ref = datetime(2026, 11, 16, 23, 46)
    week = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    pts = [datetime(2026, 11, 15, 23, 51) + (datetime(2026, 11, 16, 23, 41) - datetime(2026, 11, 15, 23, 51)) * i / 142
           for i in range(143)]
    pts = [p.replace(second=0, microsecond=0) for p in pts]
    out = T.parse_minute_labels([f"{p:%m-%d} {week[p.weekday()]} {p:%H:%M}分" for p in pts], ref)
    assert out == pts and None not in out


def test_md_label_variants():
    ref = datetime(2026, 12, 26, 15, 30)
    assert T.parse_md_label("12-23 16时", ref) == datetime(2026, 12, 23, 16)
    assert T.parse_md_label("12-23 16:57分", ref) == datetime(2026, 12, 23, 16, 57)
    assert T.parse_md_label("11-27", ref) == datetime(2026, 11, 27)
    # 实测格式：日期和小时之间夹着星期几（以前会丢掉小时，整天的数据都算成零点）
    assert T.parse_md_label("12-23 周三 16时", ref) == datetime(2026, 12, 23, 16)
    assert T.parse_md_label("12-23 星期日 05时", ref) == datetime(2026, 12, 23, 5)
    assert T.parse_md_label("abc", ref) is None


def test_md_label_cross_year_both_directions():
    # 1 月初看 12 月的历史 → 去年
    assert T.parse_md_label("12-20", datetime(2027, 1, 5)) == datetime(2026, 12, 20)
    # 12 月底看 1 月的预测 → 明年
    assert T.parse_md_label("01-01 03时", datetime(2026, 12, 31, 22)) == datetime(2027, 1, 1, 3)


def test_md_label_feb29_in_non_leap_year():
    assert T.parse_md_label("02-29", datetime(2028, 3, 5)) == datetime(2028, 2, 29)
    assert T.parse_md_label("02-29", datetime(2027, 3, 5)) == datetime(2028, 2, 29)  # 只有闰年候选合法

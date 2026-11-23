//! A stored time as Python's `datetime.fromisoformat` reads it, moved to UTC.
//!
//! A memory's `updated` is checked only for being text when its row is read, and a store
//! may hold values earlier versions imported (`2026-08-31T00:00:00Z`), so the diagnosis
//! reads it by this wider grammar. It is the grammar of Python 3.14, followed step by
//! step, its oddities included: the text is walked as bytes, a NUL ends it wherever the
//! original looks for the end of a C string, and a few malformed endings are let through.

use super::helpers::{civil_from_days, days_from_civil};

const DAY: i64 = 86_400_000_000;

/// Why a text is no instant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refused {
    /// Not a date and time in the grammar, or not one of the calendar.
    Invalid,
    /// A time without a zone.
    Naive,
    /// The time, moved to UTC, lies outside the years 0001 to 9999.
    OutOfRange,
}

/// The byte at `at`, a NUL past the end: the original reads a C string.
fn byte(text: &[u8], at: usize) -> u8 {
    text.get(at).copied().unwrap_or(0)
}

/// `count` ASCII digits from `*at` on, as a number; `*at` moves past them.
fn digits(text: &[u8], at: &mut usize, count: usize) -> Option<i64> {
    let mut number = 0;
    for _ in 0..count {
        let digit = byte(text, *at).wrapping_sub(b'0');
        if digit > 9 {
            return None;
        }
        number = number * 10 + i64::from(digit);
        *at += 1;
    }
    Some(number)
}

/// Where the date ends: the place of the character between the date and the time.
fn date_end(text: &[u8]) -> Option<usize> {
    let len = text.len();
    if len == 7 {
        return Some(7);
    }
    if text[4] == b'-' {
        if text[5] != b'W' {
            return Some(10); // YYYY-MM-DD
        }
        if len > 8 && text[8] == b'-' {
            // YYYY-Www-D, or YYYY-Www and a time that starts with "-"
            return match len {
                9 => None,
                _ if len > 10 && text[10].is_ascii_digit() => Some(8),
                _ => Some(10),
            };
        }
        return Some(8); // YYYY-Www
    }
    if text[4] != b'W' {
        return Some(8); // YYYYMMDD
    }
    // YYYYWww or YYYYWwwD: as far as the digits go
    let mut end = 7;
    while end < len && text[end].is_ascii_digit() {
        end += 1;
    }
    Some(match end {
        ..9 => end,
        _ if end % 2 == 0 => 7,
        _ => 8,
    })
}

/// The day of the week of a day counted since 1970: Monday is 0.
fn weekday(days: i64) -> i64 {
    (days + 3).rem_euclid(7)
}

fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

/// The day an ISO year, week and day of the week name, as days since 1970.
fn iso_week_day(year: i64, week: i64, day: i64) -> Option<i64> {
    if !(1..=9999).contains(&year) {
        return None;
    }
    let first = days_from_civil(year, 1, 1);
    let first_weekday = weekday(first);
    // a year has a week 53 when it starts on a Thursday, or on a Wednesday of a leap year
    let long = first_weekday == 3 || (first_weekday == 2 && is_leap(year));
    if !((1..=52).contains(&week) || (week == 53 && long)) || !(1..=7).contains(&day) {
        return None;
    }
    let mut week_one = first - first_weekday;
    if first_weekday > 3 {
        week_one += 7;
    }
    Some(week_one + (week - 1) * 7 + day - 1)
}

/// The date written in `text[..end]`, as days since 1970; any year the forms can spell.
fn date(text: &[u8], end: usize) -> Option<i64> {
    let mut at = 0;
    let year = digits(text, &mut at, 4)?;
    let dashed = byte(text, at) == b'-';
    at += usize::from(dashed);
    let dash = |at: &mut usize| {
        if dashed {
            *at += 1;
            return byte(text, *at - 1) == b'-';
        }
        true
    };
    if byte(text, at) == b'W' {
        at += 1;
        let week = digits(text, &mut at, 2)?;
        let mut day = 1;
        if at < end {
            if !dash(&mut at) {
                return None;
            }
            day = digits(text, &mut at, 1)?;
        }
        return iso_week_day(year, week, day);
    }
    let month = digits(text, &mut at, 2)?;
    if !dash(&mut at) {
        return None;
    }
    let day = digits(text, &mut at, 2)?;
    let days = days_from_civil(year, month, day);
    ((1..=12).contains(&month) && civil_from_days(days) == (year, month, day)).then_some(days)
}

/// `HH[[:]MM[[:]SS[(.|,)f+]]]` in `text[from..end]`: hour, minute, second, microsecond,
/// and whether something was left unread. None for what is no such time.
fn clock(text: &[u8], from: usize, end: usize) -> Option<([i64; 4], bool)> {
    let mut parts = [0; 4];
    let mut at = from;
    let mut colons = true;
    for index in 0..3 {
        parts[index] = digits(text, &mut at, 2)?;
        let next = byte(text, at);
        at += 1;
        if index == 0 {
            colons = next == b':';
        }
        if at >= end {
            return Some((parts, next != 0));
        }
        if colons && next == b':' {
            if index == 2 {
                return None;
            }
        } else if next == b'.' || next == b',' {
            if index < 2 {
                return None;
            }
            break;
        } else if !colons {
            at -= 1;
        } else {
            return None;
        }
    }
    // at most six digits count; the rest are read and dropped
    let counted = (end - at).min(6);
    parts[3] = digits(text, &mut at, counted)?;
    for _ in counted..6 {
        parts[3] *= 10;
    }
    while byte(text, at).is_ascii_digit() {
        at += 1;
    }
    Some((parts, byte(text, at) != 0))
}

/// Microseconds since 1970 of `text` read as `datetime.fromisoformat` reads it and moved
/// to UTC.
pub fn utc_micros(text: &str) -> Result<i64, Refused> {
    let text = text.as_bytes();
    if text.len() < 7 {
        return Err(Refused::Invalid);
    }
    let end = date_end(text).ok_or(Refused::Invalid)?;
    let mut days = date(text, end).ok_or(Refused::Invalid)?;
    if text.len() <= end {
        // a date alone is midnight without a zone
        return Err(match civil_from_days(days).0 {
            1..=9999 => Refused::Naive,
            _ => Refused::Invalid,
        });
    }
    // one character stands between the date and the time, whatever it is
    let from = end
        + match text[end] {
            0..0x80 => 1,
            0xe0..0xf0 => 3,
            0xf0.. => 4,
            _ => 2,
        };
    let len = text.len();
    let mut zone = from;
    loop {
        if matches!(byte(text, zone), b'Z' | b'+' | b'-') {
            break;
        }
        zone += 1;
        if zone >= len {
            break;
        }
    }
    let ([mut hour, minute, second, micro], left) =
        clock(text, from, zone).ok_or(Refused::Invalid)?;
    // None: no zone. Else its offset from UTC in microseconds
    let offset = if zone == len {
        if left {
            return Err(Refused::Invalid);
        }
        None
    } else if byte(text, zone) == b'Z' {
        if byte(text, zone + 1) != 0 {
            return Err(Refused::Invalid);
        }
        Some(0)
    } else {
        let sign = if text[zone] == b'-' { -1 } else { 1 };
        let Some(([hours, minutes, seconds, micros], false)) = clock(text, zone + 1, len) else {
            return Err(Refused::Invalid);
        };
        // an offset of no whole second is UTC, whatever fraction follows it
        match hours * 3600 + minutes * 60 + seconds {
            0 => Some(0),
            whole => Some(sign * (whole * 1_000_000 + micros)),
        }
    };
    if hour == 24 && minute == 0 && second == 0 && micro == 0 {
        // midnight at the end of the day is the start of the next
        hour = 0;
        days += 1;
    }
    let in_calendar = (1..=9999).contains(&civil_from_days(days).0);
    if !in_calendar || hour > 23 || minute > 59 || second > 59 {
        return Err(Refused::Invalid);
    }
    let Some(offset) = offset else {
        return Err(Refused::Naive);
    };
    if offset.abs() >= DAY {
        return Err(Refused::Invalid);
    }
    let local = days * DAY + ((hour * 60 + minute) * 60 + second) * 1_000_000 + micro;
    let utc = local - offset;
    match civil_from_days(utc.div_euclid(DAY)).0 {
        1..=9999 => Ok(utc),
        _ => Err(Refused::OutOfRange),
    }
}

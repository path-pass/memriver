//! A memory's stored `updated` value as the diagnosis reads it: every recorded text is
//! refused, for the reason Python has, or placed at the instant the Python implementation
//! (on Python 3.14) gives it.
//! `compat/record_updated_matrix.py` records the file; the cross-implementation suite
//! records it again and compares.
//!
//! On demand, a larger recording:
//!
//!     MEMRIVER_UPDATED_SAMPLES=<file> cargo test -p memriver-core --test updated_matrix -- --ignored

use memriver_core::models::helpers::format_micros;
use memriver_core::models::isoformat::{Refused, utc_micros};

fn agree(samples: &str) -> (usize, usize) {
    let (mut read, mut refused) = (0, 0);
    for line in samples.lines() {
        let sample: serde_json::Value = serde_json::from_str(line).expect("a sample line");
        let text = sample["text"].as_str().expect("text");
        match utc_micros(text) {
            Ok(micros) => {
                let stamp = format_micros(micros).expect("an instant of the calendar");
                let stamp = stamp.trim_end_matches('Z');
                assert_eq!(Some(stamp), sample["utc"].as_str(), "{text:?}");
                read += 1;
            }
            Err(why) => {
                let why = match why {
                    Refused::Invalid => "invalid",
                    Refused::Naive => "naive",
                    Refused::OutOfRange => "out-of-range",
                };
                assert!(sample["utc"].is_null(), "{text:?} is {}", sample["utc"]);
                assert_eq!(Some(why), sample["refused"].as_str(), "{text:?}");
                refused += 1;
            }
        }
    }
    (read, refused)
}

#[test]
fn every_recorded_updated_value_is_read_as_python_reads_it() {
    let (read, refused) = agree(include_str!("fixtures/updated_matrix.jsonl"));
    assert_eq!(read + refused, 3234);
    assert!(
        read > 100 && refused > 100,
        "{read} read, {refused} refused"
    );
}

#[test]
#[ignore = "reads the samples MEMRIVER_UPDATED_SAMPLES names"]
fn every_generated_updated_value_is_read_as_python_reads_it() {
    let path = std::env::var("MEMRIVER_UPDATED_SAMPLES").expect("MEMRIVER_UPDATED_SAMPLES");
    let (read, refused) = agree(&std::fs::read_to_string(path).expect("the samples file"));
    println!("{read} read, {refused} refused: all as Python");
}

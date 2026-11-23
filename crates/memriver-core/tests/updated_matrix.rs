//! A memory's stored `updated` value as the diagnosis reads it: every recorded text is
//! refused or placed at the instant the Python implementation (on Python 3.14) gives it.
//! `compat/record_updated_matrix.py` records the file; the cross-implementation suite
//! records it again and compares.
//!
//! On demand, a larger recording:
//!
//!     MEMRIVER_UPDATED_SAMPLES=<file> cargo test -p memriver-core --test updated_matrix -- --ignored

use memriver_core::models::helpers::format_micros;
use memriver_core::models::isoformat::utc_micros;

fn agree(samples: &str) -> (usize, usize) {
    let (mut read, mut refused) = (0, 0);
    for line in samples.lines() {
        let sample: serde_json::Value = serde_json::from_str(line).expect("a sample line");
        let text = sample["text"].as_str().expect("text");
        let found = utc_micros(text).ok().map(|micros| {
            let stamp = format_micros(micros).expect("an instant of the calendar");
            stamp.trim_end_matches('Z').to_string()
        });
        assert_eq!(found.as_deref(), sample["utc"].as_str(), "{text:?}");
        match found {
            Some(_) => read += 1,
            None => refused += 1,
        }
    }
    (read, refused)
}

#[test]
fn every_recorded_updated_value_is_read_as_python_reads_it() {
    let (read, refused) = agree(include_str!("fixtures/updated_matrix.jsonl"));
    assert_eq!(read + refused, 3200);
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

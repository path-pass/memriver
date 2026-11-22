//! A store named by a relative path is the one below the working directory, whatever
//! the path looks like. Alone in its file: the test moves the process's working
//! directory, which no other test may see.

use std::path::Path;

use memriver_core::repository::sqlite::database::{DATABASE_FILENAME, Database};
use memriver_test_support::Sandbox;

#[test]
fn a_relative_root_that_looks_like_a_uri_is_the_directory_below_the_working_directory() {
    let sandbox = Sandbox::new();
    std::env::set_current_dir(sandbox.dir.path()).unwrap();
    // SQLite would take a name that starts like this for a URI and open another file
    let database = Database::new(Path::new("file:rel"));
    let wrote = database.write(true, |conn| {
        conn.execute(
            "INSERT INTO projects (id, name, root, is_global) VALUES ('0000000000', 'n', NULL, 0)",
            [],
        )?;
        Ok(())
    });
    assert_eq!(wrote, Ok(()));
    let read = database.read(|conn| {
        let conn = conn.expect("the store that was just written");
        Ok(conn.query_row("SELECT name FROM projects", [], |row| {
            row.get::<_, String>(0)
        })?)
    });
    assert_eq!(read, Ok("n".to_string()));
    assert_eq!(database.schema_version(), Ok(Some(6)));

    let here = std::env::current_dir().unwrap();
    let file = here.join("file:rel").join(DATABASE_FILENAME);
    assert!(std::fs::metadata(&file).unwrap().len() > 0);
    // and nothing else was made: no file named after what a URI would have meant
    let mut made: Vec<_> = std::fs::read_dir(&here)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    made.sort();
    assert_eq!(made, ["file:rel", "home"]);
    let inside: Vec<_> = std::fs::read_dir(here.join("file:rel"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(inside, [DATABASE_FILENAME]);
}

//! The memriver crate's settings: its fixed values, the executor keys, the Codex
//! provider-override whitelist -- the exact line of every refusal -- and the `[classifier]`
//! table, which answers as the Python implementation does, case by recorded case.

use std::fs;

use memriver::settings::{
    self, ClassifierSettings, CodexOverrides, Executor, ExecutorSettings, OverrideValue,
    check_classifier_table, check_codex_overrides, check_executor_table, load_classifier_settings,
};
use memriver_core::settings::SettingsError;
use memriver_test_support::Sandbox;
use toml::de::{DeTable, DeValue};

/// What a pasted credential could look like.
fn pasted() -> String {
    format!("sk-{}", "q".repeat(24))
}

const JEV: &str = "[classifier]\nexecutor = \"jev\"\n";
const CLAUDE: &str = "[classifier]\nexecutor = \"claude\"\nexecutor_path = \"/opt/bin/claude\"\n";
const PROVIDER: &str = r#"{ model_provider = "foundry", model = "deployment-a",
    "model_providers.foundry.name" = "Foundry",
    "model_providers.foundry.base_url" = "https://example.invalid/openai/v1",
    "model_providers.foundry.env_key" = "FOUNDRY_API_KEY",
    "model_providers.foundry.wire_api" = "responses",
    "model_providers.foundry.requires_openai_auth" = false }"#;

/// `check_codex_overrides` of a value written as TOML.
fn overrides(written: &str) -> Result<CodexOverrides, String> {
    let text = format!("v = {written}\n");
    let document = DeTable::parse(&text).unwrap().into_inner();
    let (_, value) = document.iter().next().unwrap();
    check_codex_overrides(value.get_ref())
}

/// The fields `check_executor_table` names for a table written as TOML; none when it reads it.
fn executor_fields(text: &str) -> Vec<String> {
    let table = DeTable::parse(text).unwrap().into_inner();
    check_executor_table(&table).err().unwrap_or_default()
}

fn executor_table(text: &str) -> ExecutorSettings {
    check_executor_table(&DeTable::parse(text).unwrap().into_inner()).unwrap()
}

fn classifier_table(text: &str) -> Result<ClassifierSettings, Vec<String>> {
    check_classifier_table(&DeTable::parse(text).unwrap().into_inner())
}

/// A sandbox whose store directory exists, with `text` as its settings file.
fn store(text: Option<&[u8]>) -> Sandbox {
    let sandbox = Sandbox::new();
    fs::create_dir(&sandbox.root).unwrap();
    if let Some(text) = text {
        fs::write(sandbox.root.join("settings.toml"), text).unwrap();
    }
    sandbox
}

fn load(text: &str) -> Result<Option<ClassifierSettings>, SettingsError> {
    load_classifier_settings(&store(Some(text.as_bytes())).root)
}

fn read(text: &str) -> ClassifierSettings {
    load(text).unwrap().expect("a classifier table")
}

fn text(value: &str) -> OverrideValue {
    OverrideValue::Str(value.to_string())
}

#[test]
fn the_fixed_values_of_the_executors_live_here() {
    assert_eq!(
        (
            settings::KILL_GRACE_S,
            settings::DREAM_SCRATCH_PREFIX,
            settings::CLASSIFIER_SCRATCH_PREFIX,
            settings::DEFAULT_API_KEY_ENV
        ),
        (
            2,
            "memriver-dream-",
            "memriver-classifier-",
            "TYPESAFE_API_KEY"
        )
    );
}

#[test]
fn a_whitelisted_provider_is_returned_as_it_is() {
    let pairs = [
        ("model_provider", text("foundry")),
        ("model", text("deployment-a")),
        ("model_providers.foundry.name", text("Foundry")),
        (
            "model_providers.foundry.base_url",
            text("https://example.invalid/openai/v1"),
        ),
        ("model_providers.foundry.env_key", text("FOUNDRY_API_KEY")),
        ("model_providers.foundry.wire_api", text("responses")),
        (
            "model_providers.foundry.requires_openai_auth",
            OverrideValue::Bool(false),
        ),
    ];
    let expected: CodexOverrides = pairs
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect();
    assert_eq!(overrides(PROVIDER), Ok(expected));
    assert_eq!(overrides("{}"), Ok(Vec::new()));
}

#[test]
fn overrides_outside_the_whitelist_are_refused_without_echoing_values() {
    let pasted = pasted();
    let cases = [
        r#"{ "features.hooks" = false }"#.to_string(),
        r#"{ "mcp_servers.sentinel.command" = "sh" }"#.to_string(),
        r#"{ web_search = "live" }"#.to_string(),
        r#"{ model_instructions_file = "/tmp/other.md" }"#.to_string(),
        r#"{ project_doc_max_bytes = "1000" }"#.to_string(),
        // a whole table
        r#"{ model_providers = { foundry = { name = "Foundry" } } }"#.to_string(),
        r#"{ model_provider = "foundry", "model_providers.foundry" = { name = "Foundry" } }"#
            .to_string(),
        // prefix only
        r#"{ model_provider = "foundry", "model_providers.foundry.name.extra" = "x" }"#.to_string(),
        format!(
            r#"{{ model_provider = "foundry", "model_providers.foundry.experimental_bearer_token" = "{pasted}" }}"#
        ),
        format!(
            r#"{{ model_provider = "foundry", "model_providers.foundry.http_headers.api-key" = "{pasted}" }}"#
        ),
        format!(
            r#"{{ model_provider = "foundry", "model_providers.foundry.query_params.key" = "{pasted}" }}"#
        ),
        "{ model = true }".to_string(),
        r#"{ model = "two\nlines" }"#.to_string(),
        r#"{ model = " " }"#.to_string(),
        format!(
            r#"{{ model_provider = "foundry", "model_providers.foundry.env_key" = "{pasted}" }}"#
        ),
        r#"{ model_provider = "foundry", "model_providers.foundry.requires_openai_auth" = "false" }"#
            .to_string(),
        r#"{ model_provider = "foundry", "model_providers.foundry.requires_openai_auth" = 1 }"#
            .to_string(),
        r#"{ model_provider = "foundry", "model_providers.foundry.wire_api" = "chat" }"#
            .to_string(),
        format!(
            r#"{{ model_provider = "foundry", "model_providers.foundry.base_url" = "https://u:{pasted}@example.invalid/v1" }}"#
        ),
        format!(
            r#"{{ model_provider = "foundry", "model_providers.foundry.base_url" = "https://example.invalid/v1?key={pasted}" }}"#
        ),
        r#"{ model_provider = "foundry", "model_providers.foundry.base_url" = "https://example.invalid/#x" }"#
            .to_string(),
        r#"{ model_provider = "foundry", "model_providers.foundry.base_url" = "file:///etc/hosts" }"#
            .to_string(),
        // not the selected one
        r#"{ model_provider = "a", "model_providers.b.name" = "B" }"#.to_string(),
        r#"{ model_provider = "a", "model_providers.a.name" = "A", "model_providers.b.name" = "B" }"#
            .to_string(),
        // nothing selects it
        r#"{ "model_providers.a.name" = "A" }"#.to_string(),
        r#"["model", "x"]"#.to_string(),
    ];
    assert_eq!(cases.len(), 26);
    for case in cases {
        let line = overrides(&case).expect_err(&case);
        assert!(line.starts_with("codex_overrides"), "{case}");
        assert!(!line.contains(&pasted), "{case}");
    }
}

#[test]
fn each_refusal_is_its_fixed_line() {
    let pasted = pasted();
    let cases = [
        (
            r#"["model", "x"]"#.to_string(),
            "codex_overrides must be a table",
        ),
        (
            r#"{ "features.hooks" = false }"#.to_string(),
            "codex_overrides: features.hooks is not an allowed key",
        ),
        (
            r#"{ "model_providers.a b.name" = "x" }"#.to_string(),
            "codex_overrides: a key is not an allowed key",
        ),
        (
            r#"{ model_provider = "a", "model_providers.a.requires_openai_auth" = "false" }"#
                .to_string(),
            "codex_overrides: model_providers.a.requires_openai_auth must be true or false",
        ),
        (
            r#"{ model = "two\nlines" }"#.to_string(),
            "codex_overrides: model must be a non-empty single-line string",
        ),
        (
            "{ model = true }".to_string(),
            "codex_overrides: model must be a non-empty single-line string",
        ),
        (
            format!(
                r#"{{ model_provider = "a", "model_providers.a.base_url" = "https://example.invalid/v1?key={pasted}" }}"#
            ),
            "codex_overrides: model_providers.a.base_url must be an http(s) URL without user \
             information, query or fragment",
        ),
        (
            format!(r#"{{ model_provider = "a", "model_providers.a.env_key" = "{pasted}" }}"#),
            "codex_overrides: model_providers.a.env_key must name an environment variable",
        ),
        (
            r#"{ model_provider = "a", "model_providers.a.wire_api" = "chat" }"#.to_string(),
            "codex_overrides: model_providers.a.wire_api must be \"responses\"",
        ),
        (
            r#"{ model_provider = "a", "model_providers.b.name" = "B" }"#.to_string(),
            "codex_overrides: provider keys must define the one provider model_provider selects",
        ),
    ];
    for (case, line) in cases {
        assert_eq!(overrides(&case), Err(line.to_string()), "{case}");
    }
}

#[test]
fn the_executor_keys_are_read_with_their_defaults() {
    let table = executor_table("executor = \"codex\"\nexecutor_path = \"/opt/bin/codex\"\n");
    assert_eq!(
        table,
        ExecutorSettings {
            executor: Executor::Codex,
            executor_path: Some("/opt/bin/codex".to_string()),
            model: None,
            claude_settings: None,
            codex_overrides: Vec::new(),
            api_key_env: "TYPESAFE_API_KEY".to_string(),
        }
    );
    assert_eq!(executor_table("executor = \"jev\"\n").executor_path, None);
}

#[test]
fn every_bad_executor_key_is_named_together_in_field_order() {
    let pasted = pasted();
    let cases: [(String, &[&str]); 12] = [
        ("executor = \"gpt\"\n".to_string(), &["executor"]),
        (String::new(), &["executor"]),
        ("executor = \"claude\"\n".to_string(), &["executor_path"]),
        ("executor = \"codex\"\n".to_string(), &["executor_path"]),
        (
            "executor = \"claude\"\nexecutor_path = \"bin/claude\"\n".to_string(),
            &["executor_path"],
        ),
        (
            "executor = \"jev\"\nclaude_settings = \"auth.json\"\n".to_string(),
            &["claude_settings"],
        ),
        ("executor = \"jev\"\nmodel = \"\"\n".to_string(), &["model"]),
        (
            "executor = \"jev\"\nmodel = \"two\\nlines\"\n".to_string(),
            &["model"],
        ),
        (
            "executor = \"jev\"\napi_key_env = \"1BAD\"\n".to_string(),
            &["api_key_env"],
        ),
        (
            format!("executor = \"jev\"\napi_key_env = \"{pasted} x\"\n"),
            &["api_key_env"],
        ),
        (
            format!(
                "executor = \"jev\"\ncodex_overrides = {{ \"features.hooks\" = \"{pasted}\" }}\n"
            ),
            &["codex_overrides"],
        ),
        (
            "executor = \"gpt\"\nmodel = \" \"\nclaude_settings = \"rel\"\ncodex_overrides = []\n\
             api_key_env = \"1x\"\n"
                .to_string(),
            &[
                "executor",
                "model",
                "claude_settings",
                "codex_overrides",
                "api_key_env",
            ],
        ),
    ];
    for (table, fields) in cases {
        assert_eq!(executor_fields(&table), fields, "{table}");
    }
}

#[test]
fn no_file_or_no_table_means_no_classifier_settings() {
    assert_eq!(load_classifier_settings(&store(None).root), Ok(None));
    assert_eq!(load("max_body_chars = 42\n"), Ok(None));
    // and no store directory at all
    assert_eq!(load_classifier_settings(&Sandbox::new().root), Ok(None));
}

#[test]
fn a_jev_table_is_read_with_its_defaults() {
    let table = read(JEV);
    assert_eq!(
        (table.enabled, table.agent_writes, table.dream_writes),
        (true, true, true)
    );
    assert_eq!(
        (
            table.executor,
            table.executor_path.as_deref(),
            table.model.as_deref(),
            table.claude_settings.as_deref()
        ),
        (Executor::Jev, None, None, None)
    );
    assert_eq!(
        (
            table.api_key_env.as_str(),
            table.block_threshold,
            table.timeout()
        ),
        ("TYPESAFE_API_KEY", 0.7, 10)
    );
    assert_eq!(table.codex_overrides, Vec::new());
    assert_eq!(table.timeout_s, None);
}

#[test]
fn a_headless_executor_defaults_to_a_sixty_second_timeout() {
    let table = read(&format!("{CLAUDE}model = \"haiku\"\n"));
    assert_eq!(
        (
            table.executor_path.as_deref(),
            table.model.as_deref(),
            table.timeout()
        ),
        (Some("/opt/bin/claude"), Some("haiku"), 60)
    );
    assert_eq!(read(&format!("{CLAUDE}timeout_s = 5\n")).timeout(), 5);
    let codex = "[classifier]\nexecutor = \"codex\"\nexecutor_path = \"/opt/bin/codex\"\n";
    assert_eq!(read(codex).timeout(), 60);
    assert_eq!(read(&format!("{JEV}timeout_s = 5\n")).timeout(), 5);
}

#[test]
fn each_switch_set_to_false_is_read_as_false() {
    let table = read(&format!(
        "{JEV}enabled = false\nagent_writes = false\ndream_writes = false\n"
    ));
    assert_eq!(
        (table.enabled, table.agent_writes, table.dream_writes),
        (false, false, false)
    );
    // each is its own key
    for (key, expected) in [
        ("enabled", (false, true, true)),
        ("agent_writes", (true, false, true)),
        ("dream_writes", (true, true, false)),
    ] {
        let table = read(&format!("{JEV}{key} = false\n"));
        assert_eq!(
            (table.enabled, table.agent_writes, table.dream_writes),
            expected,
            "{key}"
        );
    }
}

/// An unknown key has no field to be read into: the struct holds the known ones only.
#[test]
fn keys_match_case_insensitively_and_unknown_keys_are_ignored() {
    let table = read("[classifier]\nExecutor = \"jev\"\nexecutor = \"claude\"\nunknown = 1\n");
    assert_eq!(table.executor, Executor::Jev);
}

#[test]
fn the_old_key_names_are_unknown_keys() {
    // backend and jev_model were renamed before any release: no alias
    let refused = load("[classifier]\nbackend = \"jev\"\n").unwrap_err();
    assert_eq!(refused.fields, ["classifier.executor"]);
    let table = read(&format!("{JEV}jev_model = \"jev-1\"\n"));
    assert_eq!(table.model, None);
}

#[test]
fn codex_overrides_are_read_from_their_subtable() {
    let table = read(
        "[classifier]\nexecutor = \"codex\"\nexecutor_path = \"/opt/bin/codex\"\n\
         [classifier.codex_overrides]\n\"model_provider\" = \"azure\"\n\
         \"model_providers.azure.env_key\" = \"AZURE_KEY\"\n",
    );
    assert_eq!(
        table.codex_overrides,
        [
            ("model_provider".to_string(), text("azure")),
            (
                "model_providers.azure.env_key".to_string(),
                text("AZURE_KEY")
            ),
        ]
    );
}

#[test]
fn an_invalid_table_raises_naming_only_the_file_and_the_field() {
    let pasted = pasted();
    let cases = [
        (
            "[classifier]\nexecutor = \"gpt\"\n".to_string(),
            "classifier.executor",
        ),
        (
            "[classifier]\nenabled = true\n".to_string(),
            "classifier.executor",
        ),
        (
            "[classifier]\nexecutor = \"claude\"\n".to_string(),
            "classifier.executor_path",
        ),
        (
            "[classifier]\nexecutor = \"codex\"\n".to_string(),
            "classifier.executor_path",
        ),
        (
            "[classifier]\nexecutor = \"claude\"\nexecutor_path = \"bin/claude\"\n".to_string(),
            "classifier.executor_path",
        ),
        (
            format!("{CLAUDE}claude_settings = \"auth.json\"\n"),
            "classifier.claude_settings",
        ),
        (format!("{CLAUDE}model = \"\"\n"), "classifier.model"),
        (format!("{JEV}timeout_s = 0\n"), "classifier.timeout_s"),
        (format!("{JEV}timeout_s = true\n"), "classifier.timeout_s"),
        (
            format!("{JEV}block_threshold = 0\n"),
            "classifier.block_threshold",
        ),
        (
            format!("{JEV}block_threshold = 1.5\n"),
            "classifier.block_threshold",
        ),
        (
            format!("{JEV}block_threshold = true\n"),
            "classifier.block_threshold",
        ),
        (
            format!("{JEV}api_key_env = \"1BAD\"\n"),
            "classifier.api_key_env",
        ),
        (
            format!("{JEV}api_key_env = \"{pasted} x\"\n"),
            "classifier.api_key_env",
        ),
        (format!("{JEV}model = \"\"\n"), "classifier.model"),
        (
            format!("{JEV}[classifier.codex_overrides]\n\"features.hooks\" = \"{pasted}\"\n"),
            "classifier.codex_overrides",
        ),
        ("classifier = 5\n".to_string(), "classifier"),
        (format!("{JEV}enabled = 1\n"), "classifier.enabled"),
        (format!("{JEV}enabled = \"no\"\n"), "classifier.enabled"),
        (
            format!("{JEV}agent_writes = 1\n"),
            "classifier.agent_writes",
        ),
        (
            format!("{JEV}agent_writes = \"no\"\n"),
            "classifier.agent_writes",
        ),
        (
            format!("{JEV}dream_writes = 1\n"),
            "classifier.dream_writes",
        ),
        (
            format!("{JEV}dream_writes = \"no\"\n"),
            "classifier.dream_writes",
        ),
    ];
    assert_eq!(cases.len(), 23);
    for (text, field) in cases {
        let sandbox = store(Some(format!("max_body_chars = 42\n{text}").as_bytes()));
        let refused = load_classifier_settings(&sandbox.root).unwrap_err();
        assert_eq!(
            refused,
            SettingsError {
                unreadable: false,
                env_vars: Vec::new(),
                fields: vec![field.to_string()],
            },
            "{text}"
        );
        let sentence = refused.to_string();
        assert_eq!(sentence, format!("settings.toml is invalid: field {field}"));
        // nothing chained that could carry the value
        assert!(std::error::Error::source(&refused).is_none());
        assert!(!sentence.contains(&pasted));
        assert!(!sentence.contains(sandbox.root.to_str().unwrap()));
        assert!(!format!("{refused:?}").contains(&pasted));
    }
}

#[test]
fn every_bad_classifier_key_is_named_together_in_one_line() {
    // the executor keys come first, then the classifier's own, whatever the file's order
    let refused = load(
        "[classifier]\nenabled = 1\nagent_writes = 1\ndream_writes = 1\nexecutor = \"gpt\"\n\
         model = \" \"\nclaude_settings = \"rel\"\ntimeout_s = 0\napi_key_env = \"1x\"\n\
         block_threshold = 2\n[classifier.codex_overrides]\n\"features.hooks\" = false\n",
    )
    .unwrap_err();
    assert_eq!(
        refused.fields,
        [
            "classifier.executor",
            "classifier.model",
            "classifier.claude_settings",
            "classifier.codex_overrides",
            "classifier.api_key_env",
            "classifier.enabled",
            "classifier.agent_writes",
            "classifier.dream_writes",
            "classifier.timeout_s",
            "classifier.block_threshold"
        ]
    );
    assert!(!refused.unreadable && refused.env_vars.is_empty());
}

#[test]
fn an_unreadable_file_is_unreadable() {
    let refused = load("[classifier\n").unwrap_err();
    assert!(refused.unreadable);
    assert_eq!(refused.to_string(), "settings.toml could not be read");
    // and a settings file that is no file
    let sandbox = store(None);
    fs::create_dir(sandbox.root.join("settings.toml")).unwrap();
    assert!(
        load_classifier_settings(&sandbox.root)
            .unwrap_err()
            .unreadable
    );
}

/// The error of `check_classifier_table` is the list of the invalid fields, without a prefix.
#[test]
fn check_classifier_table_raises_a_validation_error() {
    assert_eq!(
        classifier_table("executor = \"claude\"\n").unwrap_err(),
        ["executor_path"]
    );
    assert_eq!(
        classifier_table("EXECUTOR = \"jev\"\n").unwrap().executor,
        Executor::Jev
    );
}

#[test]
fn the_classifier_defaults_live_here_and_back_the_fields() {
    let table = classifier_table("executor = \"jev\"\n").unwrap();
    assert_eq!(
        (
            settings::DEFAULT_API_KEY_ENV,
            settings::DEFAULT_BLOCK_THRESHOLD
        ),
        (table.api_key_env.as_str(), table.block_threshold)
    );
    assert_eq!(
        (
            settings::DEFAULT_API_KEY_ENV,
            settings::DEFAULT_BLOCK_THRESHOLD
        ),
        ("TYPESAFE_API_KEY", 0.7)
    );
    assert_eq!(
        (
            settings::DEFAULT_HEADLESS_TIMEOUT_S,
            settings::DEFAULT_JEV_TIMEOUT_S,
            settings::DEFAULT_JEV_MODEL,
            settings::JEV_BASE_URL,
            settings::CLASSIFIER_TABLE
        ),
        (
            60,
            10,
            "jev-latest",
            "https://api.typesafe.ai",
            "classifier"
        )
    );
}

// --- the recorded matrix

/// What a table holds, a number larger than the field held as the field's largest.
#[derive(Debug, PartialEq)]
struct Values {
    executor: String,
    executor_path: Option<String>,
    model: Option<String>,
    claude_settings: Option<String>,
    codex_overrides: Vec<(String, serde_json::Value)>,
    api_key_env: String,
    switches: [bool; 3],
    timeout_s: Option<u64>,
    block_threshold: u64,
    timeout: u64,
}

/// No table, a table's values, or a refusal: unreadable, the fields, the sentence.
type Answer = Result<Option<Values>, (bool, Vec<String>, String)>;

/// A recorded number in a field of 64 bits. One the recording could only write in
/// hexadecimal is larger than the field.
fn held(recorded: &serde_json::Value) -> Option<u64> {
    let recorded = recorded.as_str()?;
    let value = recorded.parse::<u128>().unwrap_or(u128::MAX);
    Some(u64::try_from(value).unwrap_or(u64::MAX))
}

fn recorded_values(values: &serde_json::Value) -> Values {
    let text = |key: &str| values[key].as_str().map(str::to_string);
    let switch = |key: &str| values[key].as_bool().unwrap();
    let threshold: f64 = values["block_threshold"].as_str().unwrap().parse().unwrap();
    Values {
        executor: text("executor").unwrap(),
        executor_path: text("executor_path"),
        model: text("model"),
        claude_settings: text("claude_settings"),
        codex_overrides: values["codex_overrides"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
        api_key_env: text("api_key_env").unwrap(),
        switches: [
            switch("enabled"),
            switch("agent_writes"),
            switch("dream_writes"),
        ],
        timeout_s: held(&values["timeout_s"]),
        block_threshold: threshold.to_bits(),
        timeout: held(&values["timeout"]).unwrap(),
    }
}

fn read_values(table: ClassifierSettings) -> Values {
    Values {
        executor: match table.executor {
            Executor::Claude => "claude",
            Executor::Codex => "codex",
            Executor::Jev => "jev",
        }
        .to_string(),
        timeout: table.timeout(),
        executor_path: table.executor_path,
        model: table.model,
        claude_settings: table.claude_settings,
        codex_overrides: table
            .codex_overrides
            .into_iter()
            .map(|(key, value)| match value {
                OverrideValue::Str(text) => (key, serde_json::Value::String(text)),
                OverrideValue::Bool(flag) => (key, serde_json::Value::Bool(flag)),
            })
            .collect(),
        api_key_env: table.api_key_env,
        switches: [table.enabled, table.agent_writes, table.dream_writes],
        timeout_s: table.timeout_s,
        block_threshold: table.block_threshold.to_bits(),
    }
}

fn recorded_answer(answer: &serde_json::Value) -> Answer {
    if answer.is_null() {
        return Ok(None);
    }
    if let Some(values) = answer.get("values") {
        return Ok(Some(recorded_values(values)));
    }
    let fields = answer["fields"].as_array().unwrap().iter();
    Err((
        answer["unreadable"].as_bool().unwrap(),
        fields
            .map(|field| field.as_str().unwrap().to_string())
            .collect(),
        answer["error"].as_str().unwrap().to_string(),
    ))
}

/// What a marked case says this implementation answers: the values of a bare jev table with
/// those of "reads" in their place, the fields named, or an unreadable file.
fn marked_answer(rust: &serde_json::Value, bare: &serde_json::Value) -> Answer {
    if let Some(reads) = rust.get("reads") {
        let mut values = bare.clone();
        for (key, value) in reads.as_object().unwrap() {
            values[key] = value.clone();
        }
        return Ok(Some(recorded_values(&values)));
    }
    if rust.get("unreadable").is_some() {
        return Err((
            true,
            Vec::new(),
            "settings.toml could not be read".to_string(),
        ));
    }
    let fields: Vec<String> = rust["fields"]
        .as_array()
        .unwrap()
        .iter()
        .map(|field| field.as_str().unwrap().to_string())
        .collect();
    let sentence = format!("settings.toml is invalid: field {}", fields.join(", "));
    Err((false, fields, sentence))
}

fn bytes_of(case: &serde_json::Value) -> Option<Vec<u8>> {
    if let Some(text) = case["file"].as_str() {
        return Some(text.as_bytes().to_vec());
    }
    let hex = case["file_hex"].as_str()?;
    Some(
        (0..hex.len() / 2)
            .map(|at| u8::from_str_radix(&hex[2 * at..2 * at + 2], 16).unwrap())
            .collect(),
    )
}

/// What `check_codex_overrides` itself says of the `codex_overrides` of the case's table:
/// the sentence, or None when it takes it.
fn overrides_line(file: &str) -> Option<String> {
    let document = DeTable::parse(file).unwrap().into_inner();
    let table = document
        .iter()
        .find(|(key, _)| key.get_ref() == "classifier")
        .map(|(_, value)| value.get_ref());
    let Some(DeValue::Table(table)) = table else {
        panic!("no classifier table");
    };
    let value = table
        .iter()
        .find(|(key, _)| key.get_ref().to_lowercase() == "codex_overrides")
        .map(|(_, value)| value.get_ref())
        .unwrap();
    match check_codex_overrides(value) {
        Err(line) => Some(line),
        Ok(read) => {
            let DeValue::Table(written) = value else {
                panic!("what is no table was read");
            };
            let keys: Vec<&str> = written.keys().map(|key| key.get_ref().as_ref()).collect();
            let kept: Vec<&str> = read.iter().map(|(key, _)| key.as_str()).collect();
            assert_eq!(kept, keys);
            None
        }
    }
}

/// Every case of the matrix recorded from the Python implementation by
/// `compat/record_classifier_matrix.py`. A case answers as Python did, a number too large
/// for its field becoming the field's largest; where this implementation is meant to answer
/// differently the case says what and why, and that is what is expected -- and Python's
/// answer must then really be another.
#[test]
fn classifier_matrix_equals_python() {
    let cases: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/classifier_matrix.json")).unwrap();
    let cases = cases.as_array().unwrap();
    assert_eq!(cases.len(), 1676);
    let bare = cases
        .iter()
        .find(|case| case["name"] == "a bare jev table")
        .map(|case| case["answer"]["values"].clone())
        .unwrap();
    let (mut different, mut lines) = (0, 0);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let sandbox = store(bytes_of(case).as_deref());
        let got: Answer = match load_classifier_settings(&sandbox.root) {
            Ok(table) => Ok(table.map(read_values)),
            Err(refused) => {
                assert!(refused.env_vars.is_empty(), "{name}");
                let sentence = refused.to_string();
                Err((refused.unreadable, refused.fields, sentence))
            }
        };
        let python = recorded_answer(&case["answer"]);
        let Some(here) = case.get("rust") else {
            assert_eq!(got, python, "{name}");
            if let Some(line) = case.get("overrides_line") {
                let file = case["file"].as_str().unwrap();
                assert_eq!(overrides_line(file).as_deref(), line.as_str(), "{name}");
                lines += 1;
            }
            continue;
        };
        let expected = marked_answer(here, &bare);
        assert!(
            case["why"].as_str().is_some_and(|why| !why.is_empty()),
            "{name}"
        );
        assert_eq!(got, expected, "{name}");
        // a case is marked only where the two really differ
        assert_ne!(python, expected, "{name}");
        different += 1;
    }
    assert_eq!((different, lines), (30, 555));
}

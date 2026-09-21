use std::{fs, process::Command};

#[test]
fn default_search_requires_jev_while_explicit_regex_is_local() {
    let corpus = tempfile::tempdir().unwrap();
    fs::write(corpus.path().join("manual.txt"), "retention is 37 days\n").unwrap();
    let binary = env!("CARGO_BIN_EXE_gptgrep");
    let indexed = Command::new(binary)
        .arg("index")
        .arg(corpus.path())
        .arg("--json")
        .env_remove("OPENROUTER_API_KEY")
        .output()
        .unwrap();
    assert!(indexed.status.success());

    let default = Command::new(binary)
        .args(["search", "retention"])
        .arg(corpus.path())
        .arg("--json")
        .env_remove("OPENROUTER_API_KEY")
        .output()
        .unwrap();
    assert_eq!(default.status.code(), Some(2));
    let error: serde_json::Value = serde_json::from_slice(&default.stdout).unwrap();
    assert_eq!(error["code"], "jev_search_failed");
    assert_eq!(error["retrieval"]["stage"], "initialization");
    assert_eq!(error["retrieval"]["metrics"]["jev_requests"], 0);
    assert_eq!(error["retrieval"]["metrics"]["jev_calls_attempted"], 0);
    assert_eq!(error["retrieval"]["accounting_complete"], false);
    assert!(
        error["error"]
            .as_str()
            .unwrap()
            .contains("OPENROUTER_API_KEY")
    );
    assert!(
        error.get("hits").is_none(),
        "No fallback success is allowed"
    );

    let exact = Command::new(binary)
        .args(["search", "retention"])
        .arg(corpus.path())
        .args(["--mode", "regex", "--json"])
        .env_remove("OPENROUTER_API_KEY")
        .output()
        .unwrap();
    assert!(exact.status.success());
    let found: serde_json::Value = serde_json::from_slice(&exact.stdout).unwrap();
    assert_eq!(found["mode"], "regex");
    assert_eq!(found["hits"].as_array().unwrap().len(), 1);
    assert_eq!(found["metrics"]["jev_requests"], 0);
}

#[test]
fn grep_flags_do_not_silently_change_the_default_hybrid_query() {
    let result = Command::new(env!("CARGO_BIN_EXE_gptgrep"))
        .args(["search", "retention", "--fixed-strings", "--json"])
        .env_remove("OPENROUTER_API_KEY")
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(2));
    let error: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert!(
        error["error"]
            .as_str()
            .unwrap()
            .contains("require --mode regex")
    );
}

#[cfg(unix)]
#[test]
fn ask_without_jev_credentials_cannot_start_codex() {
    use std::os::unix::fs::PermissionsExt;
    let corpus = tempfile::tempdir().unwrap();
    fs::write(
        corpus.path().join("guide.txt"),
        "The service window is 23 hours.\n",
    )
    .unwrap();
    let binary = env!("CARGO_BIN_EXE_gptgrep");
    let indexed = Command::new(binary)
        .arg("index")
        .arg(corpus.path())
        .arg("--json")
        .env_remove("OPENROUTER_API_KEY")
        .output()
        .unwrap();
    assert!(indexed.status.success());
    let launcher = corpus.path().join("codex-marker.sh");
    fs::write(
        &launcher,
        "#!/bin/sh\ntouch \"$GPTGREP_LAUNCH_MARKER\"\nexit 99\n",
    )
    .unwrap();
    fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
    let marker = corpus.path().join("unexpected-launch");
    let result = Command::new(binary)
        .args(["ask", "What is the service window?"])
        .arg(corpus.path())
        .arg("--codex-bin")
        .arg(launcher)
        .arg("--codex-home")
        .arg(corpus.path())
        .arg("--json")
        .env("GPTGREP_LAUNCH_MARKER", &marker)
        .env_remove("OPENROUTER_API_KEY")
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(2));
    assert!(
        !marker.exists(),
        "Codex must not start after failed required Jev retrieval"
    );
    let failure: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(failure["ok"], false);
    assert_eq!(failure["host_retrieval"]["jev"]["requests"], 0);
    assert_eq!(
        failure["host_retrieval"]["jev"]["accounting_complete"],
        false
    );
    assert!(failure.get("answer").is_none());
}

/// Validate before opening the data directory or starting any service. Removed
/// or misspelled options must not silently fall through to normal startup.
pub fn validate_args(args: impl IntoIterator<Item = String>) -> anyhow::Result<()> {
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" | "--check-integrity" | "--repair-integrity" => {}
            "--data-dir" | "--host" | "--port" => {
                let value = args
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("{arg} needs a value"))?;
                anyhow::ensure!(
                    !value.trim().is_empty() && !value.starts_with('-'),
                    "{arg} needs a value"
                );
                if arg == "--port" {
                    value
                        .parse::<u16>()
                        .map_err(|_| anyhow::anyhow!("invalid port: {value}"))?;
                }
            }
            _ if arg.starts_with("--data-dir=") => {
                anyhow::ensure!(
                    !arg.trim_start_matches("--data-dir=").trim().is_empty(),
                    "--data-dir needs a value"
                );
            }
            _ => anyhow::bail!("unsupported argument: {arg}; use --help for supported options"),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::validate_args;

    fn validate(args: &[&str]) -> anyhow::Result<()> {
        validate_args(args.iter().map(|s| s.to_string()))
    }

    #[test]
    fn accepts_normal_startup_and_maintenance_options() {
        for args in [
            vec![],
            vec![
                "--data-dir",
                "./data",
                "--host",
                "127.0.0.1",
                "--port",
                "8081",
            ],
            vec!["--data-dir=./data", "--check-integrity"],
            vec!["--data-dir", "./data", "--repair-integrity"],
            vec!["--help"],
        ] {
            validate(&args).unwrap();
        }
    }

    #[test]
    fn rejects_removed_unknown_or_incomplete_options_before_startup() {
        for args in [
            vec!["--profile", "legacy"],
            vec!["--profile=legacy"],
            vec!["--unknown"],
            vec!["--data-dir"],
            vec!["--data-dir="],
            vec!["--data-dir", "--repair-integrity"],
            vec!["--port", "invalid"],
        ] {
            assert!(validate(&args).is_err(), "{args:?}");
        }
    }
}

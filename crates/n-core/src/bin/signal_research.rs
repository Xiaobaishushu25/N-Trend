#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let train = args.iter().any(|a| a == "--train");
    n_core::v3::research::run(args, train).await
}

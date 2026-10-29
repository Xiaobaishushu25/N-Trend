#[tokio::main]
async fn main() -> anyhow::Result<()> {
    n_core::quality::research::run(std::env::args().collect()).await
}

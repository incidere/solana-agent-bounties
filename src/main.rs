use axum::{routing::into_service, Router};
use std::net::SocketAddr;

mod yard;

#[tokio::main]
async fn main() {
    // Build the application with the yard router.
    let app = Router::new().nest("", yard::router());

    // Bind to 0.0.0.0:3000 – the same port used in the README examples.
    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    println!("Listening on {}", addr);
    axum::Server::bind(&addr)
        .serve(app.into_make_service())
        .await
        .unwrap();
}

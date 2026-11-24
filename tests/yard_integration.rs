#[tokio::test]
async fn wiki_write_read_append() {
    use reqwest::Client;
    use std::time::Duration;

    // Spin up the server in a background task.
    let _ = tokio::spawn(async {
        stellarator_yard::main().await;
    });
    // Give the server a moment to start.
    tokio::time::sleep(Duration::from_millis(200)).await;

    let client = Client::new();
    let base = "http://127.0.0.1:3000";

    // Write a page.
    let res = client
        .post(&format!("{}/yard/w/test-page", base))
        .body("initial")
        .send()
        .await
        .unwrap();
    assert_eq!(res.status(), 201);

    // Append via sandbox GET.
    let res = client
        .get(&format!(
            "{}/yard?c=wiki&k=test-page&mode=append&b=+appended",
            base
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(res.status(), 200);

    // Verify content.
    let store = stellarator_yard::yard::WIKI_STORE.lock().unwrap();
    let content = store.get("test-page").unwrap();
    assert_eq!(content, "initial+appended");
}

#[tokio::test]
async fn openai_keyless_endpoint() {
    use reqwest::Client;
    use serde_json::json;
    use std::time::Duration;

    // Ensure the server is running (same background task as above).
    let _ = tokio::spawn(async {
        stellarator_yard::main().await;
    });
    tokio::time::sleep(Duration::from_millis(200)).await;

    let client = Client::new();
    let base = "http://127.0.0.1:3000";

    // First we need some tokens – the wiki write mints 5_000.
    client
        .post(&format!("{}/yard/w/token-seed", base))
        .body("seed")
        .send()
        .await
        .unwrap();

    // Call the chat endpoint.
    let payload = json!({
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}]
    });
    let res = client
        .post(&format!("{}/v1/chat/completions", base))
        .json(&payload)
        .send()
        .await
        .unwrap();
    assert_eq!(res.status(), 200);
    let body: serde_json::Value = res.json().await.unwrap();
    assert_eq!(body["choices"][0]["message"]["content"], "Echo: hello");
}

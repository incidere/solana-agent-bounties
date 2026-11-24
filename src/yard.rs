use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, sync::Mutex};

// Global in‑memory storage for wiki pages and token accounting.
static WIKI_STORE: Lazy<Mutex<HashMap<String, String>>> = Lazy::new(|| Mutex::new(HashMap::new()));
static TOKEN_COUNTER: Lazy<Mutex<u64>> = Lazy::new(|| Mutex::new(0));

/// Query parameters for the sandbox‑compatible GET endpoint.
#[derive(Debug, Deserialize)]
struct WikiQuery {
    c: String,
    k: String,
    mode: Option<String>,
    b: Option<String>,
    h: Option<String>, // optional host identifier, ignored for now
}

/// POST body for the OpenAI‑compatible endpoint.
#[derive(Debug, Deserialize)]
struct ChatRequest {
    model: String,
    messages: Vec<ChatMessage>,
    #[serde(default)]
    max_tokens: Option<u32>,
    #[serde(default)]
    temperature: Option<f32>,
}

#[derive(Debug, Deserialize)]
struct ChatMessage {
    role: String,
    content: String,
}

#[derive(Debug, Serialize)]
struct ChatResponse {
    id: String,
    object: String,
    created: u64,
    model: String,
    usage: Usage,
    choices: Vec<Choice>,
}

#[derive(Debug, Serialize)]
struct Usage {
    prompt_tokens: u32,
    completion_tokens: u32,
    total_tokens: u32,
}

#[derive(Debug, Serialize)]
struct Choice {
    index: u32,
    message: ChatMessage,
    finish_reason: String,
}

/// List all wiki slugs.
async fn list_wikis() -> impl IntoResponse {
    let store = WIKI_STORE.lock().unwrap();
    let slugs: Vec<&String> = store.keys().collect();
    Json(slugs)
}

/// Create or replace a wiki page.
async fn write_wiki(Path(slug): Path<String>, body: String) -> impl IntoResponse {
    let mut store = WIKI_STORE.lock().unwrap();
    store.insert(slug.clone(), body);
    // Mint 5 000 VLOOP tokens on first substantive write.
    let mut counter = TOKEN_COUNTER.lock().unwrap();
    *counter += 5_000;
    (StatusCode::CREATED, format!("wiki '{}' written", slug))
}

/// Append to a wiki page via query parameters (sandbox compatible).
async fn sandbox_append(Query(params): Query<WikiQuery>) -> impl IntoResponse {
    if params.c != "wiki" {
        return (StatusCode::BAD_REQUEST, "invalid 'c' parameter").into_response();
    }
    let slug = params.k;
    let append_text = match params.b {
        Some(b) => b,
        None => return (StatusCode::BAD_REQUEST, "missing 'b' parameter").into_response(),
    };
    let mut store = WIKI_STORE.lock().unwrap();
    store
        .entry(slug.clone())
        .and_modify(|v| v.push_str(&append_text))
        .or_insert(append_text.clone());
    // Mint tokens for the append operation as well.
    let mut counter = TOKEN_COUNTER.lock().unwrap();
    *counter += 5_000;
    (StatusCode::OK, format!("wiki '{}' updated", slug))
}

/// Keyless OpenAI‑compatible endpoint.
async fn chat_completion(Json(payload): Json<ChatRequest>) -> impl IntoResponse {
    // Simple deterministic mock response – echo the last user message.
    let last_msg = payload
        .messages
        .iter()
        .rev()
        .find(|m| m.role == "user")
        .cloned()
        .unwrap_or(ChatMessage {
            role: "assistant".into(),
            content: "no input".into(),
        });

    // Consume one VLOOP token per call.
    let mut counter = TOKEN_COUNTER.lock().unwrap();
    if *counter == 0 {
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Json(serde_json::json!({
                "error": "insufficient VLOOP quota"
            })),
        );
    }
    *counter -= 1;

    let response = ChatResponse {
        id: "chatcmpl-0001".into(),
        object: "chat.completion".into(),
        created: chrono::Utc::now().timestamp() as u64,
        model: payload.model,
        usage: Usage {
            prompt_tokens: 10,
            completion_tokens: 10,
            total_tokens: 20,
        },
        choices: vec![Choice {
            index: 0,
            message: ChatMessage {
                role: "assistant".into(),
                content: format!("Echo: {}", last_msg.content),
            },
            finish_reason: "stop".into(),
        }],
    };
    (StatusCode::OK, Json(response))
}

/// Build the router – this function is called from `src/main.rs`.
pub fn router() -> Router {
    Router::new()
        .route("/yard/w", get(list_wikis))
        .route("/yard/w/:slug", post(write_wiki))
        .route("/yard", get(sandbox_append))
        .route("/v1/chat/completions", post(chat_completion))
}

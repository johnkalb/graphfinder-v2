import os, json, requests

def extract_structured_claim(raw_text: str) -> dict:
    google_key = None
    env_path = "C:/Users/johnk/AppData/Local/hermes/.env"
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                if k.strip() == "GOOGLE_API_KEY":
                    google_key = v.strip().strip("'").strip('"')
                    break
                    
    if not google_key:
        raise Exception("Google API key not found.")
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={google_key}"
    
    prompt = f"""
Analyze the following unstructured raw text containing an AI chat transcript or proof of a connection between two entities.
Extract the core relationship connection.

Format the output strictly as a JSON object matching this schema:
{{
  "subject": "The main person or organization A (proper capitalization, e.g. Bonnie R. Cohen)",
  "predicate": "The exact relationship predicate from this controlled list: FAMILY, FRIEND, EMPLOYMENT, CO_DIRECTOR, CO_OFFICER, CO_EXECUTIVE, MEMBERSHIP, ADVISORY, DONATION, LOBBYING, TRAVEL_MET, PUBLIC_OFFICE",
  "object": "The secondary person or organization B (proper capitalization, e.g. Louis R. Cohen)",
  "source_name": "The authoritative source publication, book, record, or site (e.g. U.S. Senate Confirmation Questionnaire)",
  "source_url": "The source URL link if mentioned in text (or empty string if not found)",
  "snippet": "A brief quote, sentence, or snippet from the text proving this connection"
}}

Ensure the returned JSON is valid and matches the fields exactly. If multiple relationships are mentioned, extract the strongest or most specific one.

Text to analyze:
---
{raw_text}
---
"""

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    if response.status_code != 200:
        raise Exception(f"Gemini API error: {response.text}")
        
    data = response.json()
    try:
        content_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(content_text.strip())
    except Exception as e:
        raise Exception(f"Failed to parse model output: {response.text}")

if __name__ == "__main__":
    copilot_text = """
Ah — that explains the dead ends. Once you know they’re married, the search space changes completely.

And yes: **there *is* a publicly documented link between Louis R. Cohen and Bonnie R. Cohen — they are spouses.**

Here’s what can be confirmed from **public, citable sources**:

---

# ✅ **Publicly Verifiable Link: Louis R. Cohen is married to Bonnie R. Cohen**

### **1. U.S. Senate Confirmation Questionnaire (Public Record)**  
When Bonnie R. Cohen was nominated to be **Under Secretary of State for Management**, she filed the standard **Senate Committee on Foreign Relations questionnaire**.

In her filing, she lists:

- **Spouse: Louis R. Cohen**
"""
    result = extract_structured_claim(copilot_text)
    print(json.dumps(result, indent=2))

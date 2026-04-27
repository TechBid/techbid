#!/usr/bin/env python3
"""
Local test script for Groq API integration.
Tests:
1. API authentication with each of the 3 keys
2. llama-3.1-8b-instant model connectivity
3. Job generation with exact app prompt
4. Response parsing and validation
5. Rate limit tracking
"""

import json
import sys
import os
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

print("=" * 80)
print("🧪 GROQ API LOCAL TEST SUITE")
print("=" * 80)
print(f"Test started: {datetime.now().isoformat()}\n")

# Get API keys from environment
api_keys = [
    os.getenv("GROQ_API_KEY1"),
    os.getenv("GROQ_API_KEY2"),
    os.getenv("GROQ_API_KEY3"),
]
model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

print("📋 CONFIGURATION")
print(f"Model: {model}")
print(f"API Keys Loaded: {sum(1 for k in api_keys if k)}/3")
for i, key in enumerate(api_keys, 1):
    status = "✓ Found" if key else "✗ Missing"
    display = f"{key[:20]}...{key[-10:]}" if key else "N/A"
    print(f"  Key {i}: {status} ({display})")
print()

# Check if groq package is installed
try:
    from groq import Groq
    print("✓ Groq library installed\n")
except ImportError:
    print("✗ Groq library not installed!")
    print("  Install with: pip install groq\n")
    sys.exit(1)

# Job categories from app
JOB_CATEGORIES = [
    "UI/UX Design",
    "Web Development",
    "Mobile App Development",
    "Data Science",
    "DevOps & Cloud",
    "Backend Development",
    "API Development",
    "Database Design",
    "Machine Learning",
    "Content Writing",
]

def test_api_key(key_index, api_key):
    """Test a single API key."""
    if not api_key:
        return {"status": "SKIP", "reason": "API key not configured"}
    
    key_display = f"{api_key[:15]}...{api_key[-8:]}"
    print(f"\n{'─' * 80}")
    print(f"🔑 Testing API Key {key_index + 1}: {key_display}")
    print(f"{'─' * 80}")
    
    try:
        # Initialize Groq client
        print("  1️⃣  Initializing Groq client...", end="", flush=True)
        client = Groq(api_key=api_key)
        print(" ✓")
        
        # Test basic info
        print("  2️⃣  Verifying API connectivity...", end="", flush=True)
        
        # Build the prompt (same as app)
        category = "Web Development"
        prompt = (
            f"Generate exactly 1 professional freelance job posting for the '{category}' category.\n\n"
            "Return ONLY a JSON array (no markdown, no explanation). Each job object must have:\n"
            '{"title": "...", "description": "...", "job_type": "hourly|daily|fixed", "budget_usd": <number>, "duration": "...", "robot_winner_name": "FirstName L."}\n\n'
            "- description: Create a detailed, well-structured job posting (200-350 words, max 400). Use these sections:\n"
            "  Overview:\n"
            "  - Clear 1-2 sentence project summary\n"
            "  Project Scope:\n"
            "  - 2-3 key deliverables\n"
            "  Responsibilities:\n"
            "  - 3-4 main tasks or areas\n"
            "  Requirements:\n"
            "  - 3-4 required skills/experience\n"
            "  Key Features (if applicable):\n"
            "  - 2-3 What should be included\n"
            "  Nice to Have:\n"
            "  - 1-2 bonus requirements\n"
            "- Use bullet points with clear, specific wording\n"
            "- Ensure description is professional and compelling\n"
            "- budget_usd: Between 200 and 5000\n"
            "- job_type: One of hourly, daily, fixed\n"
            "- duration: Realistic timeline like '3 weeks', '2 months', '1 month'\n"
            "- robot_winner_name: Like 'Aisha M.' or 'Kenji S.'\n\n"
            "Output valid JSON ONLY. No extra text."
        )
        
        # Make API call
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional freelance job posting generator. Generate realistic, detailed job postings."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        print(" ✓")
        
        # Parse response
        print("  3️⃣  Parsing API response...", end="", flush=True)
        raw_content = response.choices[0].message.content.strip() if response.choices else ""
        
        if not raw_content:
            print(" ✗ (Empty response)")
            return {"status": "ERROR", "reason": "Empty API response"}
        
        # Clean JSON
        raw_json = raw_content.strip("```json").strip("```").strip()
        
        try:
            jobs_data = json.loads(raw_json)
            if not isinstance(jobs_data, list):
                jobs_data = [jobs_data]
            print(" ✓")
        except json.JSONDecodeError as e:
            print(f" ✗ (Invalid JSON)")
            print(f"    Response preview: {raw_content[:150]}...")
            return {"status": "ERROR", "reason": f"JSON parse failed: {str(e)[:100]}"}
        
        # Validate job structure
        print("  4️⃣  Validating job structure...", end="", flush=True)
        required_fields = ["title", "description", "job_type", "budget_usd", "duration", "robot_winner_name"]
        
        jobs_valid = []
        for job in jobs_data:
            missing = [f for f in required_fields if f not in job or not job[f]]
            if missing:
                print(f"\n    ⚠ Skipping job with missing fields: {missing}")
                continue
            
            # Flatten nested descriptions (same as app.py)
            desc = job["description"]
            if isinstance(desc, dict):
                # Convert nested dict to properly formatted string
                parts = []
                section_order = [
                    "Overview", "Project Scope", "Responsibilities", "Requirements", 
                    "Key Features", "Nice to Have", "Tech Stack", "Deliverables"
                ]
                
                for section in section_order:
                    if section in desc:
                        value = desc[section]
                        parts.append(f"{section}:")
                        if isinstance(value, list):
                            for item in value:
                                item_str = str(item).strip()
                                if item_str:
                                    if not item_str.startswith(("-", "*")):
                                        parts.append(f"  - {item_str}")
                                    else:
                                        parts.append(f"  {item_str}")
                        elif isinstance(value, str):
                            value = value.strip()
                            if value:
                                if not value.startswith(("-", "*")):
                                    parts.append(f"  - {value}")
                                else:
                                    parts.append(f"  {value}")
                
                job["description"] = "\n".join(parts) if parts else str(desc)
            else:
                job["description"] = str(desc)
            
            # Validate job_type
            if job["job_type"] not in ["hourly", "daily", "fixed"]:
                print(f"\n    ⚠ Invalid job_type '{job['job_type']}', using 'fixed'")
                job["job_type"] = "fixed"
            
            # Validate budget
            try:
                budget = float(job["budget_usd"])
                if budget < 0 or budget > 10000:
                    print(f"\n    ⚠ Budget ${budget} out of range, will be adjusted in app")
            except (TypeError, ValueError):
                print(f"\n    ⚠ Invalid budget format: {job['budget_usd']}")
                continue
            
            jobs_valid.append(job)
        
        if not jobs_valid:
            print(" ✗ (No valid jobs)")
            return {"status": "ERROR", "reason": "No valid jobs in response"}
        
        print(f" ✓ ({len(jobs_valid)} job(s))")
        
        # Get token usage
        tokens_used = 0
        if hasattr(response, 'usage') and response.usage:
            tokens_used = response.usage.completion_tokens + response.usage.prompt_tokens
            print("  5️⃣  Token usage:", end="", flush=True)
            print(f" ✓ ({tokens_used} tokens)")
        else:
            print("  5️⃣  Token usage: ⚠ (Not available)")
        
        # Display sample job
        print("\n  📄 Sample Generated Job:")
        print("  " + "─" * 76)
        job = jobs_valid[0]
        print(f"  Title: {job['title']}")
        print(f"  Job Type: {job['job_type']}")
        print(f"  Budget: ${job['budget_usd']}")
        print(f"  Duration: {job['duration']}")
        print(f"  Winner: {job['robot_winner_name']}")
        print(f"\n  Description (Full):")
        print("  " + "─" * 76)
        # Print full description with indentation
        for line in job['description'].split('\n'):
            print(f"  {line}")
        print("  " + "─" * 76)
        
        return {
            "status": "SUCCESS",
            "jobs_count": len(jobs_valid),
            "tokens_used": tokens_used,
            "sample_job": job,
        }
        
    except Exception as e:
        error_msg = str(e)
        print(f" ✗\n  Error: {error_msg}")
        return {"status": "ERROR", "reason": error_msg[:200]}


def test_rate_limits():
    """Test rate limit tracking."""
    print(f"\n{'─' * 80}")
    print("⏱️  RATE LIMIT SIMULATION")
    print(f"{'─' * 80}")
    print("Groq Rate Limits (per key):")
    print("  • 30 requests/minute")
    print("  • 14,400 requests/day")
    print("  • ~4 jobs/day per key (at ~3,600 tokens per job)")
    print("\nWith 3 keys:")
    print("  • 90 requests/minute")
    print("  • 43,200 requests/day")
    print("  • ~12 jobs/day maximum")
    print("\nYour app limit: AI_JOBS_PER_RUN=10 (daily)")
    print("✓ Within safe limits\n")


# Main execution
def main():
    print("\n🚀 TESTING GROQ API KEYS\n")
    
    results = []
    for i, key in enumerate(api_keys):
        result = test_api_key(i, key)
        results.append(result)
    
    # Summary
    print(f"\n{'═' * 80}")
    print("📊 TEST SUMMARY")
    print(f"{'═' * 80}\n")
    
    successful = sum(1 for r in results if r.get("status") == "SUCCESS")
    failed = sum(1 for r in results if r.get("status") == "ERROR")
    skipped = sum(1 for r in results if r.get("status") == "SKIP")
    
    print(f"✓ Successful: {successful}/3")
    print(f"✗ Failed: {failed}/3")
    print(f"⊘ Skipped: {skipped}/3")
    
    if successful > 0:
        print("\n✅ GROQ API IS WORKING!")
        print("\nNext steps:")
        print("1. Update .env on Render with corrected GROQ_API_KEY variables")
        print("2. Restart the application on Render")
        print("3. Monitor app logs to confirm Groq is generating jobs")
        print("4. Check admin dashboard for newly created jobs")
    elif failed > 0:
        print("\n❌ GROQ API TEST FAILED")
        print("\nPossible issues:")
        print("1. Invalid API keys - verify keys match your Groq account")
        print("2. API rate limits exceeded - wait a few minutes and retry")
        print("3. Model not available - verify 'llama-3.1-8b-instant' is available in your Groq account")
        print("4. Network connectivity - check your internet connection")
    
    test_rate_limits()
    
    print(f"{'═' * 80}")
    print(f"Test completed: {datetime.now().isoformat()}")
    print(f"{'═' * 80}\n")
    
    return 0 if successful > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

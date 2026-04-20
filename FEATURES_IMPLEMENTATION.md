# Feature Implementation Plan - Job Notifications & Matching

## 1. Model Selection: Gemini 2.5 Flash
✅ Already using best lightweight model
- `gemini-2.5-flash`: 10x faster, lighter than standard models
- Best free tier performance
- Default in code: `gemini_model = gemini-2.5-flash`

## 2. Job Timestamps
✅ **Implementation:**
- Add `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP` to jobs table
- Display on all job cards (AI and manual)
- Format: "Posted 2 hours ago" or "Posted Apr 20, 2026 at 10:30 AM"
- Both AI jobs and robot jobs will have visible timestamps

## 3. Notification Bell System

### A. Notification Types
```
- NEW_JOB_MATCH: "New job posted matching your skills!"
- JOB_APPLICATION_UPDATE: "Your application status changed"
- ACCOUNT_UPDATE: "Your profile was updated"
- PAYMENT_CONFIRMED: "Payment received"
```

### B. Database Schema
```sql
-- Notifications table (lightweight)
CREATE TABLE notifications (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    type VARCHAR(32) NOT NULL,  -- NEW_JOB_MATCH, APPLICATION_UPDATE, etc
    title VARCHAR(255),
    message TEXT,
    job_id BIGINT,  -- nullable, for job-related notifications
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX (user_id, is_read, created_at)
);
```

### C. Bell Functionality
- **Badge**: Show unread notification count
- **Dropdown**: Show last 10 notifications
- **Mark as read**: Click notification to dismiss
- **Clear all**: Clear all notifications

## 4. Job Posting Notifications - Skill Matching

### Strategy: Multi-Method Approach (Simple to Advanced)

#### **Level 1: Simple Category Matching (Primary)**
```
User skills: ["Python", "Django", "AWS"]
Job category: "Web Development"

Match if:
- User's category subscription exists
- Job category matches user's saved categories
- No Deep Learning needed - regex/string matching
```

#### **Level 2: Keyword Extraction (If Level 1 fails)**
```
Extract keywords from:
- Job title: ["Python", "Backend", "REST"]
- Job description: parsed keywords

User skills: ["Python", "REST API"]

Match score = (matching keywords / job keywords) * 100
Notify if score > 60%
```

#### **Level 3: TF-IDF Similarity (Advanced, optional)**
```
Use scikit-learn if installed:
from sklearn.feature_extraction.text import TfidfVectorizer

Vectorize job description vs user skill text
Calculate cosine similarity
Notify if similarity > 0.65
```

### **Implementation Plan (Recommended Order):**

1. **Start with Level 1+2** (Instant results, no ML needed)
   - Category match + keyword overlap
   - ~95% accuracy for most cases
   - Zero latency

2. **Add Level 3 if needed** (Optional enhancement)
   - Use scikit-learn for TF-IDF
   - Only run for edge cases
   - Heavier computation but very accurate

### **Code Architecture:**
```python
def match_job_to_workers(job_id: int, job_title: str, job_description: str, category: str):
    """Find workers with matching skills and send notifications."""
    
    # Get all workers
    workers = store.query_all("SELECT id, skills FROM users WHERE role='worker'")
    
    for worker in workers:
        match_score = calculate_match_score(
            job_title=job_title,
            job_desc=job_description,
            job_cat=category,
            user_skills=worker['skills'],
            user_id=worker['id']
        )
        
        if match_score > MATCH_THRESHOLD:  # Default: 60%
            # Send notification
            notify_worker(worker['id'], job_id, match_score)
            # Send email if user opted in
            if user_email_notifications_enabled(worker['id']):
                send_email_notification(worker['id'], job_id)
```

## 5. Email Notifications via SMTP

### A. Email Preferences
```sql
-- Add to users table
user_preferences:
- email_job_notifications: BOOLEAN (default TRUE)
- email_frequency: ENUM('instant', 'daily', 'weekly') (default 'instant')
```

### B. Email Template
```html
Subject: New Job Match: [Job Title]

Hi [Worker Name],

A new job was posted that matches your skills!

📋 Job: [Title]
🏢 Category: [Category]
💰 Budget: [Budget]
⏰ Type: [Hourly/Daily/Fixed]

Your match score: [80%]

[Apply Now Button] → /worker/jobs/[job_id]

Posted: [Time Ago]

---
You're receiving this because you enabled job notifications.
Manage preferences: [Link to settings]
```

### C. SMTP Configuration (Already in code)
```python
smtp_host: str = _env("SMTP_HOST")
smtp_port: int = _env_int("SMTP_PORT", 587)
smtp_user: str = _env("SMTP_USER")
smtp_password: str = _env("SMTP_PASSWORD")
smtp_from_email: str = _env("SMTP_FROM_EMAIL")
```

## 6. Implementation Sequence

### Phase 1: Database & Schema (5 min)
- [ ] Add `created_at` to jobs table
- [ ] Create `notifications` table
- [ ] Add email preference fields to users

### Phase 2: Job Timestamps (5 min)
- [ ] Display on job cards
- [ ] Format relative time ("2 hours ago")

### Phase 3: Notification Bell UI (10 min)
- [ ] Add bell icon to navbar
- [ ] Show badge count
- [ ] Dropdown with notifications
- [ ] Mark as read functionality

### Phase 4: Skill Matching Engine (15 min)
- [ ] Implement Level 1 (category matching)
- [ ] Implement Level 2 (keyword extraction)
- [ ] Test with sample data

### Phase 5: Worker Notification (10 min)
- [ ] Create notification on new job
- [ ] Trigger email if opt-in
- [ ] Queue email via SMTP worker

### Phase 6: Settings Page (10 min)
- [ ] Email preference toggle
- [ ] Notification settings
- [ ] Unsubscribe option

## 7. Database Changes Summary

```sql
-- Jobs table - add timestamp
ALTER TABLE jobs ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

-- New notifications table
CREATE TABLE notifications (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    type VARCHAR(32),
    title VARCHAR(255),
    message TEXT,
    job_id BIGINT,
    match_score INT,
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_read (user_id, is_read),
    INDEX idx_created (created_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

-- Users table - add email preferences
ALTER TABLE users ADD COLUMN email_job_notifications BOOLEAN DEFAULT TRUE;
ALTER TABLE users ADD COLUMN email_notification_frequency ENUM('instant','daily','weekly') DEFAULT 'instant';
```

## 8. Estimated Complexity

| Feature | Complexity | Time | ML Required |
|---------|-----------|------|------------|
| Timestamps | ⭐ Very Easy | 5 min | ❌ |
| Notification Bell | ⭐⭐ Easy | 10 min | ❌ |
| Category Matching | ⭐⭐ Easy | 5 min | ❌ |
| Keyword Extraction | ⭐⭐ Easy | 10 min | ❌ |
| Email Integration | ⭐⭐ Easy | 10 min | ❌ |
| TF-IDF Matching | ⭐⭐⭐ Medium | 15 min | ✅ scikit-learn |

**Total: ~50 min for full implementation**

## 9. Skills Matching Examples

### Example 1: Exact Category Match
```
Job: "Build Django REST API"
Category: "Web Development"
User skills: ["Python", "Django", "REST API", "PostgreSQL"]
User categories: ["Web Development", "DevOps"]

Match: YES (100%)
Notify: YES
```

### Example 2: Keyword Overlap
```
Job: "Mobile React Native App"
Category: "Mobile Development"
User skills: ["React", "React Native", "JavaScript"]
User categories: ["Web Development"]

Match: YES (80% - React/React Native overlap)
Notify: YES
```

### Example 3: No Match
```
Job: "Blockchain Smart Contracts"
Category: "Blockchain"
User skills: ["Python", "Django", "PostgreSQL"]
User categories: ["Web Development"]

Match: NO (0% overlap)
Notify: NO
```

## 10. Performance Considerations

- **Notifications table indexed**: By user_id + is_read for fast queries
- **Matching runs async**: In background thread, doesn't block job creation
- **Email batching**: Can batch emails for 'daily' frequency users
- **Cache skill tags**: Cache common skill keywords to avoid re-parsing

## 11. Future Enhancements

- [ ] AI-powered skill matching with Gemini
- [ ] Push notifications (mobile app)
- [ ] SMS notifications
- [ ] Slack/Discord integration
- [ ] Notification frequency: Real-time, daily digest, weekly
- [ ] Advanced filtering: Min budget, job type, duration

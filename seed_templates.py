#!/usr/bin/env python3
"""
Generate realistic job templates for bulk job creation.
Run this script to seed the database with initial templates.
Usage: python seed_templates.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app import STORE

# Realistic job templates across categories
TEMPLATES = [
    # Web Development
    {
        "category": "Web Development",
        "job_type": "fixed",
        "budget_usd": 1500,
        "duration": "3-4 weeks",
        "connects_required": 25,
        "title": "Full-Stack E-Commerce Website Development",
        "description": """We need an experienced full-stack developer to build a modern e-commerce platform. 

Requirements:
- Frontend: React.js with TypeScript, Tailwind CSS for styling
- Backend: Node.js/Express.js with PostgreSQL database
- Features: User authentication, product catalog, shopping cart, payment integration (Stripe), admin dashboard
- Must implement responsive design for mobile/tablet/desktop
- API documentation with Swagger

Deliverables:
- Complete source code with git history
- Deployment guide (AWS or Vercel recommended)
- Testing suite with >80% coverage
- One month of bug fixes post-launch

Please provide portfolio with similar projects."""
    },
    {
        "category": "Web Development",
        "job_type": "hourly",
        "budget_usd": 5000,
        "duration": "Ongoing",
        "connects_required": 30,
        "title": "Next.js Performance Optimization Consultant",
        "description": """Seeking an expert Next.js developer for ongoing performance optimization and maintenance.

Responsibilities:
- Code review and optimization of existing Next.js application
- Implement caching strategies and CDN optimization
- Database query optimization and indexing
- Implement monitoring and alerting (Sentry, DataDog)
- Refactor legacy code for maintainability
- Mentor junior developers on best practices

Requirements:
- 5+ years Next.js/React experience
- Experience with performance profiling tools
- Knowledge of web performance metrics (Core Web Vitals)
- Experience with CI/CD pipelines

Rate: $85-120/hour based on experience"""
    },
    # Data Science
    {
        "category": "Data Science & Analytics",
        "job_type": "fixed",
        "budget_usd": 3000,
        "duration": "4 weeks",
        "connects_required": 35,
        "title": "Build Predictive Analytics Dashboard",
        "description": """We need a data scientist to build a predictive analytics dashboard for our SaaS platform.

Project Scope:
- Analyze historical sales data (provided in CSV format)
- Build ML models (regression, classification) using Python
- Implement time-series forecasting for revenue predictions
- Create interactive Tableau/Power BI dashboard
- Write technical documentation

Tech Stack:
- Python (pandas, scikit-learn, XGBoost)
- SQL for data queries
- Tableau or Power BI for visualization
- Git for version control

Deliverables:
- Jupyter notebooks with EDA and model development
- Production-ready Python scripts
- Dashboard with real-time data refresh
- Model performance report with metrics"""
    },
    # Mobile Development
    {
        "category": "Mobile Development",
        "job_type": "fixed",
        "budget_usd": 2500,
        "duration": "6 weeks",
        "connects_required": 28,
        "title": "React Native Fitness Tracking App",
        "description": """Develop a cross-platform fitness tracking mobile app for iOS and Android.

Features Required:
- User authentication with social login (Google, Apple)
- Workout logging and tracking with GPS integration
- Calorie counter with nutrition database
- Leaderboard and social features
- Push notifications for workout reminders
- Offline functionality with sync capability
- Integration with wearables (Apple Watch, Fitbit)

Technical Requirements:
- React Native with Expo or bare workflow
- Firebase for backend
- Redux for state management
- SQLite for local storage

Deliverables:
- Complete source code
- App store and Google Play submission guides
- 30 days of bug fixes and support"""
    },
    # Cybersecurity
    {
        "category": "Cybersecurity",
        "job_type": "fixed",
        "budget_usd": 4000,
        "duration": "2 weeks",
        "connects_required": 40,
        "title": "Security Audit & Penetration Testing",
        "description": """We need a professional penetration tester to conduct a comprehensive security audit of our web application.

Scope:
- Source code review for vulnerabilities (OWASP Top 10)
- Infrastructure security assessment
- API security testing
- Database security review
- Penetration testing (both automated and manual)
- Social engineering assessment
- Network vulnerability scanning

Deliverables:
- Detailed penetration test report with findings
- CVSS scores for each vulnerability
- Remediation recommendations with priority levels
- Proof of concept exploits (if applicable)
- Remediation verification after fixes

Requirements:
- OSCP or CEH certification preferred
- 5+ years of professional penetration testing
- Familiarity with tools: Burp Suite, Metasploit, Nessus"""
    },
    # Graphic Design
    {
        "category": "Graphic Design",
        "job_type": "fixed",
        "budget_usd": 800,
        "duration": "1 week",
        "connects_required": 15,
        "title": "Brand Identity Design Package",
        "description": """Looking for a talented graphic designer to create a complete brand identity package.

Deliverables:
- Logo design (3 variations + vector files)
- Color palette (hex codes and Pantone references)
- Typography guidelines
- Business card design
- Letterhead template
- Social media templates (Instagram, LinkedIn, Twitter)
- Brand guidelines document (10+ pages)

Style Preference:
- Modern, minimalist aesthetic
- Tech-forward but approachable
- Professional and trustworthy

Files Provided:
- Company brief and values
- Competitor analysis
- Target audience information

Requirements:
- Adobe Creative Suite proficiency
- Portfolio with 10+ brand identity projects
- Unlimited revisions until satisfied
- Editable source files (PSD, AI, XD)"""
    },
    # Content Writing
    {
        "category": "Content Writing & Copywriting",
        "job_type": "fixed",
        "budget_usd": 1200,
        "duration": "2 weeks",
        "connects_required": 20,
        "title": "SaaS Website Copywriting & SEO",
        "description": """We need an experienced SaaS copywriter to rewrite our website content with SEO optimization.

Pages to Write:
- Homepage (hero section, value propositions, CTA sections)
- Features page with comparison table
- Pricing page with tier descriptions
- 5 blog posts (1500 words each, SEO-optimized)
- Case studies (2x, customer success stories)
- FAQ section
- Email automation sequences (5 emails)

Requirements:
- SaaS experience (B2B preferred)
- SEO knowledge and keyword research skills
- Conversions copywriting expertise
- CMS experience (preferably webflow or WordPress)
- Understanding of target audience (enterprise/mid-market)

Style:
- Engaging, conversational tone
- Data-driven messaging
- Clear value propositions
- Strong CTAs

Deliverables:
- All copy in Google Docs with comments
- Keyword research document
- SEO recommendations report"""
    },
    # Video Editing
    {
        "category": "Video Editing",
        "job_type": "fixed",
        "budget_usd": 2000,
        "duration": "3 weeks",
        "connects_required": 25,
        "title": "YouTube Channel Content Editing",
        "description": """Seeking a professional video editor for ongoing YouTube channel content creation.

Project Details:
- Edit 4 videos per month (15-25 min each)
- Add animations, transitions, and effects
- Color grading and audio normalization
- Generate thumbnails with A/B testing data
- Optimize videos for YouTube algorithm
- Create shorts (60-second highlights)
- Manage YouTube SEO (titles, descriptions, tags)

Requirements:
- 3+ years professional video editing experience
- Proficiency in Premiere Pro or Final Cut Pro
- Motion graphics skills (After Effects)
- Thumbnail design skills
- Understanding of YouTube analytics
- Fast turnaround time

Deliverables:
- Edited 4K videos ready for upload
- Custom thumbnails per video
- Optimized metadata and descriptions
- Monthly performance report"""
    },
    # UI/UX Design
    {
        "category": "UI/UX Design",
        "job_type": "fixed",
        "budget_usd": 2200,
        "duration": "4 weeks",
        "connects_required": 30,
        "title": "Mobile App UI/UX Design System",
        "description": """Create a comprehensive design system and high-fidelity prototypes for our mobile app.

Deliverables:
- User research summary and personas
- Wireframes for 12+ key screens
- High-fidelity mockups (iOS and Android)
- Interactive prototypes in Figma
- Design system with components library
- Accessibility audit (WCAG compliance)
- Design documentation and handoff guide
- Design tokens export

Screens to Design:
- Onboarding flow (4 screens)
- Dashboard and analytics
- User profile and settings
- Transaction history
- Notifications center
- Search and filter interface
- Payment/checkout flow

Requirements:
- 5+ years UI/UX design experience
- Figma expertise (component system)
- Mobile app design specialization
- Accessibility knowledge
- Portfolio with similar projects"""
    },
    # QA Testing
    {
        "category": "Quality Assurance & Testing",
        "job_type": "daily",
        "budget_usd": 2000,
        "duration": "Ongoing",
        "connects_required": 22,
        "title": "QA Automation Engineer (Selenium/Cypress)",
        "description": """We're hiring a QA automation engineer for comprehensive test automation.

Responsibilities:
- Design and implement automated test suites
- Create regression test scripts
- Perform manual exploratory testing
- Set up CI/CD integration for tests
- Document test cases and results
- Participate in sprint planning and refinement
- Report bugs with detailed reproduction steps

Tech Stack:
- Selenium and/or Cypress
- Python or JavaScript
- TestNG or Jest
- Jenkins or GitHub Actions
- Jira for bug tracking

Requirements:
- 3+ years QA automation experience
- Strong programming fundamentals
- API testing experience (Postman/REST Assured)
- Database query knowledge (SQL)
- Agile/Scrum experience

Rate: $50-75 per day based on experience"""
    },
    # Blockchain/Web3
    {
        "category": "Blockchain & Web3",
        "job_type": "fixed",
        "budget_usd": 5000,
        "duration": "8 weeks",
        "connects_required": 45,
        "title": "Solidity Smart Contract Development",
        "description": """Develop production-ready smart contracts for an NFT marketplace platform.

Project Scope:
- ERC-721 and ERC-1155 token implementations
- NFT marketplace contract with escrow
- Royalty distribution mechanism
- Governance token (ERC-20) with staking
- Security audit-ready code with extensive comments
- Comprehensive test suite (Hardhat)

Requirements:
- Expert-level Solidity proficiency (0.8+)
- Ethereum protocol understanding
- Smart contract security best practices
- Experience with Hardhat or Truffle
- Gas optimization knowledge
- Experience with OpenZeppelin libraries

Deliverables:
- Audited and tested smart contracts
- Documentation and deployment guides
- Mainnet and testnet deployment scripts
- ABI and JSON interfaces
- 4 weeks of technical support"""
    },
    # Accounting
    {
        "category": "Accounting & Finance",
        "job_type": "hourly",
        "budget_usd": 3000,
        "duration": "Ongoing",
        "connects_required": 24,
        "title": "Bookkeeping & Financial Analysis",
        "description": """Looking for an experienced bookkeeper for ongoing financial management.

Tasks:
- Monthly accounts receivable/payable processing
- Reconciliation of bank accounts
- Preparation of financial statements
- Tax preparation support (estimated quarterly taxes)
- Expense categorization and reporting
- Budget vs. actual analysis
- Monthly financial dashboards
- Invoice and expense management

Requirements:
- CPA or equivalent accounting background
- 5+ years bookkeeping experience
- Proficiency in QuickBooks Online or Xero
- Excel skills for financial modeling
- Understanding of GAAP principles
- Experience with SaaS accounting

Tools:
- QuickBooks Online
- Stripe/payment processor integration
- Monthly reporting

Rate: $60-85 per hour based on volume"""
    },
    # Machine Learning
    {
        "category": "Machine Learning / AI",
        "job_type": "fixed",
        "budget_usd": 6000,
        "duration": "6 weeks",
        "connects_required": 50,
        "title": "Custom ML Model Development & Deployment",
        "description": """Build custom machine learning models with production deployment.

Project Overview:
- Problem definition and data exploration
- Feature engineering and selection
- Multiple model training and evaluation
- Hyperparameter tuning and optimization
- Model validation and cross-validation
- API deployment (FastAPI or Flask)
- Model monitoring and retraining pipeline
- Documentation and model cards

Requirements:
- 5+ years ML/data science experience
- Python expertise (scikit-learn, TensorFlow, PyTorch)
- Experience with cloud platforms (AWS/GCP/Azure)
- MLOps knowledge (Docker, Kubernetes)
- Git and version control
- Experience with model serving (TensorFlow Serving, Seldon)

Deliverables:
- Trained and optimized models
- Production-ready API
- Docker containerization
- CI/CD pipeline for retraining
- Performance monitoring dashboard
- Comprehensive documentation"""
    },
]

def seed_templates():
    """Insert templates into database."""
    try:
        table_name = STORE.t('job_templates')
        
        for template in TEMPLATES:
            # Check if template already exists
            existing = STORE.query_one(
                f"SELECT id FROM {table_name} WHERE title=%s AND category=%s",
                (template['title'], template['category'])
            )
            
            if not existing:
                STORE.execute(
                    f"INSERT INTO {table_name} (title, description, category, job_type, budget_usd, duration, connects_required, is_active) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, 1)",
                    (
                        template['title'],
                        template['description'],
                        template['category'],
                        template['job_type'],
                        template['budget_usd'],
                        template['duration'],
                        template['connects_required']
                    )
                )
                print(f"✓ Added: {template['title']}")
            else:
                print(f"⊘ Already exists: {template['title']}")
        
        total = STORE.query_one(f"SELECT COUNT(*) as c FROM {table_name}", ())
        print(f"\n✓ Total templates in database: {total['c']}")
        
    except Exception as e:
        print(f"✗ Error: {e}")
        return False
    
    return True

if __name__ == '__main__':
    print("Seeding job templates...\n")
    success = seed_templates()
    sys.exit(0 if success else 1)

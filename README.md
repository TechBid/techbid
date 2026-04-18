# TechBid Marketplace

A modern freelance marketplace platform connecting skilled professionals with opportunities. Built with Flask, MySQL, Socket.IO, and powered by Gemini AI for job generation.

## Features

- **Real-time Messaging**: Socket.IO-based live chat between employers and freelancers
- **AI Job Generation**: Automated job creation using Gemini AI with configurable daily limits
- **Fraud Prevention**: Comprehensive validation and security measures
- **Payment Processing**: Integrated Paystack and Pesapal payment gateways
- **Mobile Responsive**: Fully responsive design with professional side drawer navigation
- **Dark/Light Theme**: Theme switching with persistent user preference
- **SEO Optimized**: Structured data, meta tags, and sitemap generation
- **Role-Based Access**: Separate interfaces for workers, employers, and admins

## Tech Stack

- **Backend**: Flask 3.0+ with Flask-SocketIO 5.3+
- **Database**: MySQL 8.0+ (Aiven Cloud)
- **Real-time**: Socket.IO 4.5+ with WebSocket fallback
- **Frontend**: HTML5, CSS3, Vanilla JavaScript
- **Authentication**: Google OAuth 2.0
- **Payments**: Paystack, Pesapal
- **AI**: Google Gemini AI (for job generation)
- **Deployment**: Waitress WSGI server on Render.com

## Prerequisites

- Python 3.11+
- MySQL 8.0+ database
- Google OAuth credentials
- Paystack API keys
- Pesapal API keys
- Gemini AI API key
- GitHub token (for profile picture storage)

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/TechBid/techbid.git
   cd techbid
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

5. **Run the application**
   ```bash
   python app.py
   ```

Visit `http://localhost:5000` in your browser.

## Environment Configuration

See `.env.example` for all required variables. Key variables:

```env
# App
FLASK_SECRET=your-secret-key
PORT=5000

# MySQL
MYSQL_HOST=your-db-host
MYSQL_DATABASE=your-database
MYSQL_USER=your-user
MYSQL_PASSWORD=your-password

# Google OAuth
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret

# Gemini AI (Job Generation)
GEMINI_API_KEY=your-gemini-key
AI_JOBS_PER_RUN=10

# Payments
PAYSTACK_SECRET_KEY=your-paystack-key
PESAPAL_CONSUMER_KEY=your-pesapal-key
```

## Deployment to Render

1. **Push to GitHub** (if not already done)
   ```bash
   git add .
   git commit -m "Deploy to Render"
   git push -u origin main
   ```

2. **Create Render Service**
   - Go to [Render.com](https://render.com)
   - Click "New Web Service"
   - Connect your GitHub repository
   - Set Build Command: `pip install -r requirements.txt`
   - Set Start Command: `waitress-serve --host=0.0.0.0 --port=$PORT app:app`

3. **Set Environment Variables in Render Dashboard**
   - Copy all variables from `.env` to Render's environment settings
   - Ensure database is accessible from Render
   - Update `SERVER_NAME` and `PREFERRED_URL_SCHEME` to match your Render domain

4. **Update OAuth Redirect URIs**
   - Google Console: Add `https://your-render-domain/auth/google/callback`
   - Paystack: Update callback URL to production domain
   - Pesapal: Update callback URL to production domain

5. **Deploy**
   - Render will automatically deploy on push to `main` branch

## Project Structure

```
.
├── app.py                 # Flask application (6500+ lines)
├── requirements.txt       # Python dependencies
├── Procfile              # Render deployment config
├── runtime.txt           # Python version for Render
├── static/
│   ├── css/style.css     # Main stylesheet (responsive design)
│   ├── js/               # JavaScript files
│   └── favicon.png       # App icon
├── templates/            # HTML templates
│   ├── base.html         # Base template with navigation
│   ├── worker/           # Worker interface
│   ├── employer/         # Employer interface
│   └── admin/            # Admin dashboard
├── .env                  # Environment variables (local)
└── .env.example          # Example configuration
```

## API Endpoints

### Auth
- `POST /auth/google/callback` - Google OAuth callback
- `GET /logout` - User logout

### Jobs
- `GET /jobs` - List jobs (worker view)
- `GET /job/<id>` - Job details
- `POST /job/apply` - Apply for job
- `GET /admin/jobs` - Admin job management

### Payments
- `POST /billing/paystack/callback` - Paystack webhook
- `POST /billing/pesapal/callback` - Pesapal webhook

### WebSocket (Real-time)
- Socket.IO rooms: `contract_room_{job_id}`
- Events: `message`, `status_update`

## Database Schema

Tables include:
- `users` - User accounts
- `jobs` - Job postings (with `is_robot` flag for AI jobs)
- `applications` - Job applications
- `robot_winners` - Robot winner profiles
- `contracts` - Escrow/contract management
- `payments` - Payment records
- `messages` - Real-time chat messages

AI-generated jobs use `employer_id=NULL` and `is_robot=1` flag.

## Security

- CSRF protection on all forms
- SQL injection prevention via parameterized queries
- Password hashing with bcrypt
- JWT tokens for sensitive operations
- Rate limiting on sensitive endpoints
- HTTPS enforced on production
- Content Security Policy headers

## Monitoring & Logs

- Logs written to console (stdout)
- Key events logged: user auth, payments, job generation
- Error tracking via Flask error handlers
- Database connection pooling for performance

## Troubleshooting

### Database Connection Issues
- Verify `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD` in `.env`
- Ensure MySQL allows remote connections
- Check SSL certificate path if `MYSQL_SSL_CA` is set

### AI Job Generation Fails
- Verify `GEMINI_API_KEY` is valid
- Check API quotas in Google Console
- Review logs for error messages

### Socket.IO Connection Issues
- Ensure WebSocket is not blocked by firewall
- Check that `socketio` is listening on correct host/port
- Browser console for JS errors

### OAuth Issues
- Verify `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
- Update redirect URI in Google Console to match deployment domain
- Clear browser cookies if authentication fails

## Development

### Running Tests
```bash
# Currently no automated tests - manual testing required
```

### Code Style
- PEP 8 Python style guide
- Minimal external dependencies
- In-place SQL queries (prefer parameterized)

## Performance Optimizations

- Connection pooling for database
- Gzip compression on responses
- Client-side caching for static assets
- Real-time updates via WebSocket instead of polling
- Lazy loading for images

## License

Proprietary - TechBid Marketplace

## Support

For issues, questions, or feedback, please contact the development team.

---

**Last Updated**: April 18, 2026
**Status**: Production Ready

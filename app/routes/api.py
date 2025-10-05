from flask import Blueprint, render_template_string, request, redirect
import datetime

bp = Blueprint('api', __name__)

FORM_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Safe Line Report</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            background-color: #f8f9fa;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .form-container {
            background: #ffffff;
            padding: 2rem;
            border-radius: 1rem;
            box-shadow: 0 4px 10px rgba(0,0,0,0.1);
            max-width: 600px;
            width: 100%;
        }
        .alert {
            margin-bottom: 1rem;
        }
    </style>
</head>
<body>
    <div class="container-fluid">
        <div class="container">
            <h2 class="text-center mb-4">Verify Your Cybercrime Report</h2>
            
            {% if message %}
            <div class="alert alert-{{ message_type }} alert-dismissible fade show" role="alert">
                {{ message }}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
            {% endif %}
            
            <form method="POST" action="/submit">
                <input type="hidden" name="case_id" value="{{ case_id }}">

                <div class="mb-3">
                    <label class="form-label">Name *</label>
                    <input type="text" name="name" class="form-control" value="{{ data.name }}" required>
                </div>

                <div class="mb-3">
                    <label class="form-label">Phone *</label>
                    <input type="tel" name="phone" class="form-control" value="{{ data.phone }}" required>
                </div>

                <div class="mb-3">
                    <label class="form-label">Email</label>
                    <input type="email" name="email" class="form-control" value="{{ data.email }}">
                    <div class="form-text">We'll use this to send updates about your case.</div>
                </div>

                <div class="mb-3">
                    <label class="form-label">Crime Type</label>
                    <select name="crime_type" class="form-control">
                        <option value="">Select crime type</option>
                        <option value="scam" {% if data.crime_type == 'scam' %}selected{% endif %}>Scam/Fraud</option>
                        <option value="phishing" {% if data.crime_type == 'phishing' %}selected{% endif %}>Phishing</option>
                        <option value="hacking" {% if data.crime_type == 'hacking' %}selected{% endif %}>Account Hacking</option>
                        <option value="harassment" {% if data.crime_type == 'harassment' %}selected{% endif %}>Harassment</option>
                        <option value="doxxing" {% if data.crime_type == 'doxxing' %}selected{% endif %}>Doxxing</option>
                        <option value="other" {% if data.crime_type == 'other' %}selected{% endif %}>Other</option>
                    </select>
                </div>

                <div class="mb-3">
                    <label class="form-label">Incident Date</label>
                    <input type="date" name="incident_date" class="form-control" value="{{ data.incident_date }}">
                </div>

                <div class="mb-3">
                    <label class="form-label">Description *</label>
                    <textarea name="description" class="form-control" rows="4" required>{{ data.description }}</textarea>
                    <div class="form-text">Please provide detailed information about what happened.</div>
                </div>

                <div class="mb-3">
                    <label class="form-label">Amount Lost (optional)</label>
                    <input type="number" step="0.01" name="amount_lost" class="form-control" 
                        value="{{ data.amount_lost if data.amount_lost and data.amount_lost != 'None' else '' }}"
                        placeholder="Leave empty if no money was lost">
                    <div class="form-text">Enter the amount in your local currency. Leave blank if no financial loss occurred.</div>
                </div>

                <div class="mb-3">
                    <label class="form-label">Evidence</label>
                    <input type="text" name="evidence" class="form-control" value="{{ data.evidence }}">
                    <div class="form-text">Any screenshots, emails, or other evidence you have.</div>
                </div>

                <div class="d-grid gap-2">
                    <button type="submit" class="btn btn-primary btn-lg">Update Report</button>
                    <a href="#" class="btn btn-outline-secondary" onclick="history.back()">Cancel</a>
                </div>
            </form>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

SUCCESS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Report Updated - Safe Line</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-6">
                <div class="card shadow">
                    <div class="card-body text-center p-5">
                        <div class="mb-4">
                            <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
                                <polyline points="22 4 12 14.01 9 11.01"></polyline>
                            </svg>
                        </div>
                        <h2 class="card-title text-success">Report Updated Successfully!</h2>
                        <p class="card-text mt-3">Your cybercrime report has been updated. Our team will review it and contact you if needed.</p>
                        <p class="text-muted">Case ID: <strong>{{ case_id }}</strong></p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

@bp.route('/f/<case_id>')
def short_form(case_id: str):
    from app.services.form_service import FormService
    form_service = FormService()
    data = form_service.get_case_data_for_form(case_id)
    if not data:
        return "Case not found", 404
    
    # Format date for HTML input
    if data.get('incident_date') and isinstance(data['incident_date'], str):
        try:
            # Try to parse and format the date
            if '-' in data['incident_date']:
                date_obj = datetime.datetime.strptime(data['incident_date'], '%Y-%m-%d')
                data['incident_date'] = date_obj.strftime('%Y-%m-%d')
        except ValueError:
            pass
    
    message = request.args.get('message', '')
    message_type = request.args.get('message_type', 'info')
    
    return render_template_string(FORM_HTML, case_id=case_id, data=data, 
                                message=message, message_type=message_type)

@bp.route('/submit', methods=['POST'])
def submit_form():
    case_id = request.form['case_id']
    print(f"📝 Form submitted for {case_id}: {dict(request.form)}")
    
    from app.services.form_service import FormService
    form_service = FormService()
    
    # Update the case with form data
    success = form_service.update_case_from_form(case_id, request.form.to_dict())
    
    if success:
        return render_template_string(SUCCESS_HTML, case_id=case_id)
    else:
        return redirect(f'/f/{case_id}?message=Failed to update report. Please try again.&message_type=danger')
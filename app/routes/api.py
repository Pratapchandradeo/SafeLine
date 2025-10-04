from flask import Blueprint, render_template_string, request

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
        }
        .form-container {
            background: #ffffff;
            padding: 2rem;
            border-radius: 1rem;
            box-shadow: 0 4px 10px rgba(0,0,0,0.1);
            max-width: 600px;
            width: 100%;
        }
    </style>
</head>
<body>
    <div class="container-fluid">
        <div class="container mt-3 mb-3">
            <h2 class="text-center mb-4">Verify Your Cybercrime Report</h2>
            <form method="POST" action="/submit">
                <input type="hidden" name="case_id" value="{{ case_id }}">

                <div class="mb-3">
                    <label class="form-label">Name</label>
                    <input type="text" name="name" class="form-control" value="{{ data.name }}" required>
                </div>

                <div class="mb-3">
                    <label class="form-label">Phone</label>
                    <input type="tel" name="phone" class="form-control" value="{{ data.phone }}" required>
                </div>

                <div class="mb-3">
                    <label class="form-label">Email</label>
                    <input type="email" name="email" class="form-control" value="{{ data.email }}">
                </div>

                <div class="mb-3">
                    <label class="form-label">Crime Type</label>
                    <input type="text" name="crime_type" class="form-control" value="{{ data.crime_type }}">
                </div>

                <div class="mb-3">
                    <label class="form-label">Incident Date</label>
                    <input type="date" name="incident_date" class="form-control" value="{{ data.incident_date }}">
                </div>

                <div class="mb-3">
                    <label class="form-label">Description</label>
                    <textarea name="description" class="form-control" rows="4">{{ data.description }}</textarea>
                </div>

                {% if data.amount_lost %}
                <div class="mb-3">
                    <label class="form-label">Amount Lost</label>
                    <input type="number" name="amount_lost" class="form-control" value="{{ data.amount_lost }}">
                </div>
                {% endif %}

                <div class="mb-3">
                    <label class="form-label">Evidence</label>
                    <input type="text" name="evidence" class="form-control" value="{{ data.evidence }}">
                </div>

                <div class="d-grid">
                    <button type="submit" class="btn btn-primary btn-lg">Submit</button>
                </div>
            </form>
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
    return render_template_string(FORM_HTML, case_id=case_id, data=data)

@bp.route('/submit', methods=['POST'])
def submit_form():
    case_id = request.form['case_id']
    print(f"Form submitted for {case_id}: {request.form}")
    return "Report submitted successfully!"

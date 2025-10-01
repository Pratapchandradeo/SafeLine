from flask import Blueprint, render_template_string, request

bp = Blueprint('api', __name__)

FORM_HTML = """
<!DOCTYPE html>
<html>
<head><title>Safe Line Report</title></head>
<body>
    <h1>Verify Your Cybercrime Report</h1>
    <form method="POST" action="/submit">
        <input type="hidden" name="case_id" value="{{ case_id }}">
        <label>Name: <input type="text" name="name" value="{{ data.name }}"></label><br>
        <label>Phone: <input type="tel" name="phone" value="{{ data.phone }}"></label><br>
        <label>Email: <input type="email" name="email" value="{{ data.email }}"></label><br>
        <label>Crime Type: <input type="text" name="crime_type" value="{{ data.crime_type }}"></label><br>
        <label>Date: <input type="date" name="incident_date" value="{{ data.incident_date }}"></label><br>
        <label>Description: <textarea name="description">{{ data.description }}</textarea></label><br>
        {% if data.amount_lost %}<label>Amount Lost: <input type="number" name="amount_lost" value="{{ data.amount_lost }}"></label><br>{% endif %}
        <label>Evidence: <input type="text" name="evidence" value="{{ data.evidence }}"></label><br>
        <button type="submit">Submit</button>
    </form>
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
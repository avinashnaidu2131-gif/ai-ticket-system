"""
Expanded training script with more diverse examples and confidence calibration.
Run once: python train_model.py
"""
import os
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.model_selection import cross_val_score
import numpy as np

os.makedirs("models", exist_ok=True)

TRAINING_DATA = [
    # Billing
    ("payment failed on checkout",            "Billing"),
    ("I was charged twice",                   "Billing"),
    ("refund not received after 7 days",      "Billing"),
    ("invoice is incorrect",                  "Billing"),
    ("subscription charge error",             "Billing"),
    ("billing amount is wrong",               "Billing"),
    ("cannot complete payment",               "Billing"),
    ("credit card declined",                  "Billing"),

    # Authentication
    ("cannot login to my account",            "Authentication"),
    ("password reset link not working",       "Authentication"),
    ("OTP not received",                      "Authentication"),
    ("two factor authentication broken",      "Authentication"),
    ("account locked after failed attempts",  "Authentication"),
    ("forgot password help",                  "Authentication"),
    ("session expired keeps logging me out",  "Authentication"),
    ("login page shows error 401",            "Authentication"),

    # Technical
    ("app crashes on startup",                "Technical"),
    ("server is down",                        "Technical"),
    ("getting 500 error",                     "Technical"),
    ("feature is broken not working",         "Technical"),
    ("data not loading",                      "Technical"),
    ("bug in dashboard",                      "Technical"),
    ("API returns null response",             "Technical"),
    ("performance very slow",                 "Technical"),

    # Feature Request
    ("please add dark mode",                  "Feature Request"),
    ("can you add export to CSV",             "Feature Request"),
    ("I want a mobile app",                   "Feature Request"),
    ("feature suggestion notification bell",  "Feature Request"),
    ("request to add bulk import",            "Feature Request"),

    # General
    ("how do I get started",                  "General"),
    ("where is the documentation",            "General"),
    ("I have a question about your product",  "General"),
    ("need help understanding pricing",       "General"),
]

texts, labels = zip(*TRAINING_DATA)

# Build a pipeline (vectorizer + classifier)
pipeline = Pipeline([
    ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=5000)),
    ("clf",   LogisticRegression(max_iter=1000, C=1.0)),
])

# Quick cross-val sanity check
scores = cross_val_score(pipeline, texts, labels, cv=3, scoring="accuracy")
print(f"Cross-val accuracy: {scores.mean():.2f} ± {scores.std():.2f}")

# Fit on full data
pipeline.fit(texts, labels)

# Save components separately (classifier.py expects them split)
joblib.dump(pipeline.named_steps["clf"],   "models/classifier.pkl")
joblib.dump(pipeline.named_steps["tfidf"], "models/vectorizer.pkl")

print("✅  Model trained and saved to models/")
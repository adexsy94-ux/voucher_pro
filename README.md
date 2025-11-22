Here is a clean, professional **README.md** tailored specifically for your **VoucherPro (Streamlit + Aiven PostgreSQL)** project.
You can copy-paste it directly into a file named **`README.md`** in your GitHub repo.

---

# VoucherPro — Streamlit + Aiven PostgreSQL

VoucherPro is a complete voucher & invoice management system built with **Python**, **Streamlit**, and **PostgreSQL**.
This version is fully updated to use **Aiven PostgreSQL** as the production database and includes ready-to-run scripts for local development, migration from SQLite, and database connection testing.

---

##  Features

* Voucher creation & approval workflow
* Invoice creation with multi-currency support
* PDF generation for vouchers & invoices
* Vendor management
* CRM-lite functions (jobs, vendors, users)
* Secure user authentication
* Full PostgreSQL backend (Aiven)
* Streamlit web dashboard
* Built-in utilities for audit logs and history tracking

---

##  Project Structure

```
voucherpro/
│
├── voucherpro.py                # Main Streamlit application
├── test_aiven.py                # Aiven PostgreSQL connection test script
├── requirements.txt             # Python dependencies
├── README.md                    # Project documentation
│
└── .streamlit/
       └── secrets.toml          # Aiven credentials (not committed to GitHub)
```

---

##  Requirements

* Python 3.10+
* Streamlit Cloud (for deployment)
* Aiven PostgreSQL service
* Linux/Windows/MacOS

---

##  Installation

1. **Clone the repository**

```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPO.git
cd YOUR-REPO
```

2. **Install dependencies**

```bash
pip install -r requirements.txt
```

3. **Create `.streamlit/secrets.toml`**

```
.streamlit/
    secrets.toml
```

Paste your Aiven credentials:

```toml
[pg]
host = "pg-cb495ce-adexsy94-643a.i.aivencloud.com"
port = 14073
dbname = "defaultdb"
user = "avnadmin"
password = "AVNS_HW9bgleEeofjFFF21iW"
sslmode = "require"
```

 **Never commit this file to GitHub.**

---

##  Test Your Database Connection

Use the included `test_aiven.py` file:

```bash
python test_aiven.py
```

Expected output:

```
Connected OK. PostgreSQL version: PostgreSQL 16.x
```

If you get:

```
could not translate host name ...
```

Check that your Aiven service has **Public Access Enabled**.

---

##  Running VoucherPro

Start the app:

```bash
streamlit run voucherpro.py
```

You will see:

* Login screen
* Dashboard
* Voucher & Invoice pages
* Vendor/Jobs CRUD
* PDF generator

---

##  Deploying on Streamlit Cloud

1. Push your repo to GitHub
2. Go to [https://streamlit.io/cloud](https://streamlit.io/cloud)
3. Deploy your repo
4. Open **Settings → Secrets**
5. Paste the same `[pg]` block from your local `secrets.toml`

Streamlit Cloud will automatically install requirements and run your app.

---

##  Troubleshooting

###  Database connection error

If you see:

```
Database connection error: could not translate host name ...
```

It means:

* The Aiven host is incorrect **OR**
* Public access is disabled in Aiven **OR**
* The service is stopped / DNS not yet active

### Fix

* Open your Aiven console
* Go to **Overview → Connection Information**
* Copy the **EXACT** Service URI into `voucherpro.py`
* Ensure **Public Access = Enabled**

###  PDFs not displaying correctly

Ensure the system has fonts installed or use bundled fonts in ReportLab.

---

##  License

Private proprietary project — all rights reserved.

---

##  Support / Future Improvements

Planned improvements:

* Enhanced multi-currency invoice templates
* Improved vendor analytics
* Multi-tenant support
* Email delivery integration
* Cloud-native PDF storage and file attachments

For help or collaboration, contact the developer.

---

If you want, I can:

1 Generate a more detailed README
2 Add installation screenshots
3 Add badges (Build, Deploy, License, etc.)
4 Generate a CONTRIBUTING.md
5 Generate an API Documentation file

Just tell me!

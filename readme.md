todo
- japanese support


usage:

**auto_crawler.py**

```
python auto_crawler.py <url> [options]

  url              Seek search URL to crawl (required)
  --pages N        Max pages to crawl (default: 10)
  --delay N        Base delay multiplier between jobs (default: 2.0)
  --headless       Run browser headless, no window shown
```

Example:
```
python auto_crawler.py "https://au.seek.com/jobs?keywords=IT+Support&location=Melbourne" --pages 3 --delay 4
```

---

**advisor.py**

```
python advisor.py <report(s)> [options]

  reports          One or more .txt report file paths (required)
                   Supports globs: reports/strong_match/*.txt
  --cover          Also generate a cover letter for each report
  --platform NAME  Job platform name used in cover letter opening (default: Seek)
```

Examples:
```
python advisor.py reports/strong_match/91995152.txt
python advisor.py reports/strong_match/91995152.txt --cover
python advisor.py reports/strong_match/91995152.txt --cover --platform LinkedIn
python advisor.py reports/strong_match/*.txt --cover
```

8/6
 - added auto navigate
 - added LLM-based leter writer and suggestion

6/6
 - added skill normaisation
 - added score-based decision making
 - added report storage with grading seperation
 - implemented input queue awaiting for LLM

5/6
 - added LLM summary & user profile matching

4/6
 - inheritate from old Job Stream Repo
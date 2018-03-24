
def branches_format(s, branch, now=None):
    s = s.format(branch=branch, Branch=branch.title(), BRANCH=branch.upper())
    if now:
        s = now.strftime(s)

    return s

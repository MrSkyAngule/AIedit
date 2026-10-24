def total(items):
    return sum(items)

API_KEY = "sk-secret"
def sign(data):
    return hash(data + API_KEY)

def charge(user, amount):
    return amount * 100

def refund(user, amount):
    return -amount

def average(items):
    return total(items) / len(items)

stock = {}


def add(item, n):
    stock[item] = stock.get(item, 0) + n


add("apple", 3)

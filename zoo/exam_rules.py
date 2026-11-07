"""exam_rules (family C, held out): a hand-category rule bot, no simulation.
Preflop: plays pairs, connected or suited high cards. Postflop: bets made hands by category
(two pair or better on the flop, a straight or better by the river), chases flush draws when
the price is small, folds the rest to bets."""
from game import Action
from evaluator import category_of, score_five


def new_half(config, memory):
    memory.clear()


def _best_category(hole, board):
    """Best made-hand category using exactly 2 hole + 3 board cards (any board length >= 3)."""
    from itertools import combinations
    best = -1
    for b3 in combinations(board, 3):
        for h2 in combinations(hole, 2):
            best = max(best, category_of(score_five(list(h2) + list(b3))))
    return best


def _flush_draw(hole, board):
    for s in range(4):
        if sum((c & 3) == s for c in board) >= 2 and sum((c & 3) == s for c in hole) >= 2:
            return True
    return False


def choose_action(obs, memory):
    try:
        legal, hole, board = obs.legal_actions, obs.hole_cards, obs.board
        if not board:
            ranks = sorted(c >> 2 for c in hole)
            pair = len(set(ranks)) < 5
            connected = sum(1 for a, b in zip(ranks, ranks[1:]) if b - a == 1) >= 2
            playable = pair or connected or sum(r >= 5 for r in ranks) >= 3
            if playable:
                return Action.CHECK_CALL
            return Action.CHECK_CALL if obs.to_call == 0 else Action.FOLD
        cat = _best_category(hole, board)
        need = 2 if len(board) == 3 else 4          # two pair on the flop, straight+ later
        if cat >= need:
            for a in (Action.RAISE_HALF, Action.RAISE_POT):
                if a in legal:
                    return a
            return Action.CHECK_CALL
        if obs.to_call == 0:
            return Action.CHECK_CALL
        cheap = obs.to_call <= obs.pot // 3
        if cat >= 1 and cheap or _flush_draw(hole, board) and cheap and len(board) < 5:
            return Action.CHECK_CALL
        return Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass

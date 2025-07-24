module Components.Banner exposing (view)

import Html exposing (Html, div, h2, text)
import Html.Attributes exposing (style)

view : String -> Html msg
view bannerText =
    div [ style "background-color" "#1976d2", style "color" "white", style "padding" "20px", style "text-align" "center" ]
        [ h2 [] [ text bannerText ] ] 
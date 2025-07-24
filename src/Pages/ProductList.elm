module Pages.ProductList exposing (view)

import Html exposing (Html, div, h3, p, img, button, text, a, Attribute, nav, span, ul, li)
import Html.Attributes exposing (class, src, alt, href, style)
import Data.Products exposing (allProducts, Product)

view : Html msg
view =
    div []
        [ breadcrumb
        , div [ class "product-grid" ] (List.map productCard allProducts)
        ]

breadcrumb : Html msg
breadcrumb =
    nav [ class "breadcrumb", style "margin" "24px 0 0 0", style "font-size" "15px", style "color" "#888" ]
        [ a [ href "/", style "color" "#1976d2", style "text-decoration" "none", style "font-weight" "500" ] [ text "Home" ]
        , span [] [ text " / " ]
        , span [ style "color" "#222", style "font-weight" "600" ] [ text "Products" ]
        ]

productCard : Product -> Html msg
productCard product =
    div [ class "product-card" ]
        [ img [ src (fixImage product) , alt product.name ] []
        , h3 [] [ text product.name ]
        , p [] [ text product.description ]
        , div [ style "display" "flex", style "flex-direction" "column", style "gap" "10px", style "align-items" "center", style "margin-top" "10px" ]
            [ a [ href ("/product/" ++ String.fromInt product.id) ]
                [ button [] [ text "View Details" ] ]
            , a [ href "/calculator" ]
                [ button [ class "btn-primary" ] [ text "Calculate Loan" ] ]
            ]
        ]

fixImage : Product -> String
fixImage product =
    case product.name of
        "Car Loan" -> "https://images.unsplash.com/photo-1511918984145-48de785d4c4e?auto=format&fit=crop&w=400&q=80"
        "Business Loan" -> "https://images.unsplash.com/photo-1508385082359-f48b1c1b1f57?auto=format&fit=crop&w=400&q=80"
        _ -> product.imageUrl 